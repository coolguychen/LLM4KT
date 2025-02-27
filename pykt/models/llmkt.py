import torch
from torch import nn
from torch.nn.init import xavier_uniform_
from torch.nn.init import constant_
import math
import torch.nn.functional as F
from enum import IntEnum
import numpy as np
from .utils import transformer_FFN, ut_mask, pos_encode, get_clones
from torch.nn import Module, Embedding, LSTM, Linear, Dropout, LayerNorm, TransformerEncoder, TransformerEncoderLayer, \
        MultiLabelMarginLoss, MultiLabelSoftMarginLoss, CrossEntropyLoss, BCELoss, MultiheadAttention
from torch.nn.functional import one_hot, cross_entropy, multilabel_margin_loss, binary_cross_entropy
from .que_base_model import QueBaseModel,QueEmb
from torch.utils.checkpoint import checkpoint

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class Dim(IntEnum):
    batch = 0
    seq = 1
    feature = 2

class SPKT(nn.Module):
    def __init__(self, n_question, n_pid, 
            d_model, n_blocks, dropout, d_ff=256, 
            loss1=0.5, loss2=0.5, loss3=0.5, start=50, num_layers=2, nheads=4, seq_len=1024, 
            kq_same=1, final_fc_dim=512, final_fc_dim2=256, num_attn_heads=8, separate_qa=False, l2=1e-5, emb_type="qid", emb_path="", pretrain_dim=768, cf_weight=0.3, t_weight=0.3, local_rank=1, num_sgap=None, c0=0, max_epoch=0, q_window_size=20, c_window_size=4):
        super().__init__()
        """
        初始化函数
        """

        """
        参数说明：
            d_model: attention块的维度
            final_fc_dim: 最终全连接网络在预测前的维度
            num_attn_heads: 多头注意力中的头数
            d_ff: 基本块内部全连接网络的维度
            kq_same: 如果key和query相同，则kq_same=1，否则=0
        """
        self.model_name = "spkt"
        print(f"model_name: {self.model_name}, emb_type: {emb_type}")
        self.n_question = n_question
        self.dropout = dropout
        self.kq_same = kq_same
        self.n_pid = n_pid
        self.l2 = l2
        self.model_type = self.model_name
        self.separate_qa = separate_qa
        self.emb_type = emb_type
        self.ce_loss = BCELoss()
        self.cf_weight = cf_weight
        self.t_weight = t_weight
        self.num_sgap = num_sgap
        self.q_window_size = q_window_size
        self.c_window_size = c_window_size
        # print(f"q_window_size:{self.q_window_size}")
        # print(f"c_window_size:{self.c_window_size}")

        """
        嵌入层相关设置
        """
        self.embed_l = d_model

        self.dataset_emb = nn.Embedding(20,self.embed_l).to(device)# dataset_id embedding

        self.qa_embed = nn.Embedding(2, self.embed_l)

        if self.emb_type.find("pt") != -1:
            self.time_emb = nn.Embedding(self.num_sgap+1, self.embed_l)

        """
        架构对象，包含多个注意力块
        """
        # Architecture Object. It contains stack of attention block

        # self.emb_q = nn.Sequential(
        #     nn.Linear(1, 200000), nn.ReLU(), nn.Dropout(self.dropout),
        #     nn.Linear(200000,d_model)
        # )

        self.emb_q = nn.Embedding(200000,self.embed_l).to(device)# question embedding


        # self.emb_c = nn.Sequential(
        #     nn.Linear(7,1000), nn.ReLU(), nn.Dropout(self.dropout), # 7表示一个问题最大的concept数量
        #     nn.Linear(1000,d_model)
        # )

        self.emb_c = nn.Parameter(torch.randn(1000, self.embed_l).to(device), requires_grad=True)# kc embedding


        self.model = Architecture(n_question=n_question, n_blocks=n_blocks, n_heads=num_attn_heads, dropout=dropout,
                                    d_model=d_model, d_feature=d_model / num_attn_heads, d_ff=d_ff,  kq_same=self.kq_same, model_type=self.model_type, seq_len=seq_len)

        """
        输出层设置
        """
        self.out = nn.Sequential(
            nn.Linear(d_model + self.embed_l,
                      final_fc_dim), nn.ReLU(), nn.Dropout(self.dropout),
            nn.Linear(final_fc_dim, final_fc_dim2), nn.ReLU(
            ), nn.Dropout(self.dropout),
            nn.Linear(final_fc_dim2, 1)
        )

        """
        预测类别输出层设置
        """
        if emb_type.find("predc") != -1:
            self.qclasifier = nn.Sequential(
                nn.Linear(d_model + self.embed_l,
                        final_fc_dim), nn.ReLU(), nn.Dropout(self.dropout),
                nn.Linear(final_fc_dim, final_fc_dim2), nn.ReLU(
                ), nn.Dropout(self.dropout),
                nn.Linear(final_fc_dim2, self.n_pid)
            )

        """
        时间预测输出层设置
        """
        if emb_type.find("pt") != -1:
            self.t_out = nn.Sequential(
                nn.Linear(d_model + self.embed_l,
                        final_fc_dim), nn.ReLU(), nn.Dropout(self.dropout),
                nn.Linear(final_fc_dim, final_fc_dim2), nn.ReLU(
                ), nn.Dropout(self.dropout),
                nn.Linear(final_fc_dim2, 1)
            )

        self.reset()


    def reset(self):
        for p in self.parameters():
            if p.size(0) == self.n_pid+1 and self.n_pid > 0:
                torch.nn.init.constant_(p, 0.)

    def get_avg_skill_emb(self,c):
        # add zero for padding
        concept_emb_cat = torch.cat(
            [torch.zeros(1, self.embed_l).to(device), 
            self.emb_c], dim=0)
        # shift c

        related_concepts = (c+1).long()
        #[batch_size, seq_len, emb_dim]
        concept_emb_sum = concept_emb_cat[related_concepts, :].sum(
            axis=-2)

        #[batch_size, seq_len,1]
        concept_num = torch.where(related_concepts != 0, 1, 0).sum(
            axis=-1).unsqueeze(-1)
        concept_num = torch.where(concept_num == 0, 1, concept_num)
        concept_avg = (concept_emb_sum / concept_num)
        return concept_avg

    def forward(self, dcur, qtest=False, train=False, dgaps=None):
        # 将输入数据转换为长整型并移至设备
        q, c, r = dcur["qseqs"].long().to(device), dcur["cseqs"].long().to(device), dcur["rseqs"].long().to(device)
        qshft, cshft, rshft = dcur["shft_qseqs"].long().to(device), dcur["shft_cseqs"].long().to(device), dcur["shft_rseqs"].long().to(device)
        # print(f"q:{q.shape}")
        batch_size = q.size(0)

        # 获取数据集ID并移至设备
        dataset_id = dcur["dataset_id"].long().to(device)
        # 拼接问题和问题偏移量
        pid_data = torch.cat((q[:,0:1], qshft), dim=1) # shape[batch,200]
        # 拼接概念和概念偏移量
        q_data = torch.cat((c[:,0:1], cshft), dim=1) # shape[batch,200,7]
        # 拼接参考和参考偏移量
        target = torch.cat((r[:,0:1], rshft), dim=1)

        # 数据增强处理
        if self.emb_type.find("aug") != -1 and train:
            # 生成新的问题ID
            aug_pids= pid_data.unfold(1, self.q_window_size, 1)
            aug_pids = aug_pids.sum(dim=2)
            aug_pid_data = torch.where(aug_pids >= 200000, -1, aug_pids)

            # 生成新的概念ID
            if q_data.size(2) <  self.c_window_size:
                aug_cids = q_data.unfold(2, q_data.size(2), 1).sum(dim=-1)
            else:
                aug_cids = q_data.unfold(2, self.c_window_size, 1).sum(dim=-1)
            aug_cid_data = torch.where(aug_cids >= 1000, -1, aug_cids)
            # print(f"aug_cid_data:{aug_cid_data.shape}")

            # 生成新的参考ID
            window_counts = target.unfold(1, self.q_window_size, 1).sum(dim=2)
            aug_target_data = (window_counts > self.q_window_size / 2).float().long()

            # 对增强数据进行填充
            pad_dims = [0, pid_data.size(1) - aug_pid_data.size(1), 0, pid_data.size(0) - aug_pid_data.size(0)]
            tmp_aug_pid_data_padded = F.pad(aug_pid_data, pad_dims, value=-1)
            aug_pid_data_padded = torch.where(tmp_aug_pid_data_padded == -1, 0, tmp_aug_pid_data_padded)
            # print(f"aug_pid_data_padded:{aug_pid_data_padded.shape}")
            cid_padding_size = q_data.size(2) - aug_cid_data.size(2)
            aug_cid_data_padded = F.pad(aug_cid_data, (0, cid_padding_size))
            # print(f"aug_cid_data_padded:{aug_cid_data_padded.shape}")
            aug_target_data = F.pad(aug_target_data, pad_dims, value=0)

            # 堆叠数据
            all_pid_data = torch.cat([pid_data, aug_pid_data_padded],dim=0)
            # print(f"all_pid_data:{torch.max(all_pid_data)}")
            all_cid_data = torch.cat([q_data, aug_cid_data_padded],dim=0)
            # print(f"all_cid_data:{torch.max(all_cid_data)}")
            all_target_data = torch.cat([target, aug_target_data],dim=0)
            # print(f"all_target_data:{torch.max(all_target_data)}")

            # 嵌入处理
            emb_q = self.emb_q(all_pid_data) #[batch,max_len-1,emb_size]
            emb_c = self.get_avg_skill_emb(all_cid_data) #[batch,max_len-1,emb_size]
            dataset_embed_data = self.dataset_emb(dataset_id).unsqueeze(1).repeat(2,1,1)
            qa_embed_data = self.qa_embed(all_target_data)
        else:
            # 正常的嵌入处理
            emb_q = self.emb_q(pid_data)#[batch,max_len-1,emb_size]
            emb_c = self.get_avg_skill_emb(q_data)#[batch,max_len-1,emb_size]
            dataset_embed_data = self.dataset_emb(dataset_id).unsqueeze(1)
            # print(f"dataset_embed_data:{dataset_embed_data.shape}")
            try:
                qa_embed_data = self.qa_embed(target)
            except:
                print(f"target:{target}")

        # 时间嵌入处理
        if self.emb_type.find("pt") != -1:
            sg, sgshft = dgaps["sgaps"].long(), dgaps["shft_sgaps"].long()
            s_gaps = torch.cat((sg[:, 0:1], sgshft), dim=1)
            emb_t = self.time_emb(s_gaps)
            q_embed_data += emb_t

        # print(f"emb_q:{emb_q.shape}")
        # print(f"emb_c:{emb_c.shape}")
        # print(f"dataset_embed_data:{dataset_embed_data.shape}")
        # 拼接嵌入数据
        q_embed_data = emb_q + emb_c + dataset_embed_data
        qa_embed_data = q_embed_data + qa_embed_data

        # 处理模型输出
        y2, y3 = 0, 0
        d_output = self.model((q_embed_data, qa_embed_data))
        concat_q = torch.cat([d_output, q_embed_data], dim=-1)
        output = self.out(concat_q).squeeze(-1)
        m = nn.Sigmoid()
        preds = m(output)

        cl_losses = 0
        # 分类损失处理
        if self.emb_type.find("predc") != -1 and train:
            sm = dcur["smasks"].long()
            start = 0
            cpreds = self.qclasifier(concat_q[:,start:,:])
            # print(f"cpreds:{cpreds.shape}")
            flag = sm[:,start:]==1
            # print(f"flag:{flag.shape}")
            # print(f"cpreds:{cpreds[:,:-1,:][flag].shape}")
            # print(f"qtag:{q[:,start:][flag].shape}")
            cl_loss = self.ce_loss(cpreds[:,:-1,:][flag], q[:,start:][flag])
            cl_losses += self.cf_weight * cl_loss

        # 时间损失处理
        if self.emb_type.find("pt") != -1 and train:
            t_label= dgaps["shft_pretlabel"].double()
            t_combined = torch.cat((d_output, emb_t), -1)
            t_output = self.t_out(t_combined).squeeze(-1)
            t_pred = m(t_output)[:,1:]
            # print(f"t_pred:{t_pred}")
            sm = dcur["smasks"]
            ty = torch.masked_select(t_pred, sm)
            # print(f"min_y:{torch.min(ty)}")
            tt = torch.masked_select(t_label, sm)
            # print(f"min_t:{torch.min(tt)}")
            t_loss = binary_cross_entropy(ty.double(), tt.double())
            # t_loss = mse_loss(ty.double(), tt.double())
            # print(f"t_loss:{t_loss}")
            cl_losses += self.t_weight * t_loss
        # 数据增强损失处理
        if self.emb_type.find("aug") != -1 and train:
            # cal loss of augmented data
            aug_preds = preds[batch_size:]
            # 拆分预测结果
            preds = preds[:batch_size]

            mask_select = tmp_aug_pid_data_padded != -1
            select_preds = aug_preds[mask_select]
            select_targets = aug_target_data[mask_select]
            # print(f"select_preds:{torch.max(select_preds)}")
            # print(f"select_targets:{torch.max(select_targets)}")
            cl_losses = self.ce_loss(select_preds, select_targets.float())
            # print(f"cl_losses:{cl_losses}")

        # 根据训练状态返回不同的输出
        if train:
            if self.emb_type == "qid":
                return preds, y2, y3
            else:
                return preds, y2, y3, cl_losses
        else:
            if qtest:
                return preds, concat_q
            else:
                return preds


class Architecture(nn.Module):
    def __init__(self, n_question,  n_blocks, d_model, d_feature,
                 d_ff, n_heads, dropout, kq_same, model_type, seq_len):
        super().__init__()
        """
            n_block : number of stacked blocks in the attention
            d_model : dimension of attention input/output
            d_feature : dimension of input in each of the multi-head attention part.
            n_head : number of heads. n_heads*d_feature = d_model
        """
        self.d_model = d_model
        self.model_type = model_type

        if model_type in {'gpt4kt','spkt'}:
            self.blocks_2 = nn.ModuleList([
                TransformerLayer(d_model=d_model, d_feature=d_model // n_heads,
                                 d_ff=d_ff, dropout=dropout, n_heads=n_heads, kq_same=kq_same)
                for _ in range(n_blocks)
            ])
        self.position_emb = CosinePositionalEmbedding(d_model=self.d_model, max_len=seq_len)

    def forward(self, inputs):
        # target shape  bs, seqlen
        # 输入数据，包括问题和问答对的嵌入数据
        q_embed_data, qa_embed_data = inputs
        # 获取序列长度和批次大小
        seqlen, batch_size = q_embed_data.size(1), q_embed_data.size(0)

        # 对问题和问答对嵌入数据分别添加位置嵌入
        q_posemb = self.position_emb(q_embed_data)
        q_embed_data = q_embed_data + q_posemb
        qa_posemb = self.position_emb(qa_embed_data)
        qa_embed_data = qa_embed_data + qa_posemb

        # 保存添加了位置嵌入后的问答对嵌入数据
        qa_pos_embed = qa_embed_data
        q_pos_embed = q_embed_data

        # 将添加了位置嵌入后的问答对嵌入数据赋值给y
        y = qa_pos_embed
        # 更新序列长度和批次大小（实际上此时的大小没有变化，但为了保持代码一致性，这里还是更新一下）
        seqlen, batch_size = y.size(1), y.size(0)
        # 将添加了位置嵌入后的问题嵌入数据赋值给x
        x = q_pos_embed

        # 编码器部分

        for block in self.blocks_2:
            # 对x进行编码处理
            # x.requires_grad_(True)
            # y.requires_grad_(True)
            # def run_block(mask, query, key, values, apply_pos):
            #     return block(mask, query, key, values, apply_pos)
            # x = checkpoint(run_block, mask, x, x, y, apply_pos)

            # 使用checkpoint来减少内存消耗
            x = checkpoint(block, x, x, y)
            # x = block(mask=0, query=x, key=x, values=y, apply_pos=True) # True: +FFN+残差+laynorm 非第一层与0~t-1的的q的attention, 对应图中Knowledge Retriever
            # mask=0，不能看到当前的response, 在Knowledge Retrever的value全为0，因此，实现了第一题只有question信息，无qa信息的目的
            # print(x[0,0,:])
            # x = input_data[1]
        # 返回编码后的x
        return x


class TransformerLayer(nn.Module):
    def __init__(self, d_model, d_feature,
                 d_ff, n_heads, dropout,  kq_same):
        super().__init__()
        """
            This is a Basic Block of Transformer paper. It containts one Multi-head attention object. Followed by layer norm and postion wise feedforward net and dropout layer.
        """
        kq_same = kq_same == 1
        # Multi-Head Attention Block
        self.masked_attn_head = MultiHeadAttention(
            d_model, d_feature, n_heads, dropout, kq_same=kq_same)

        # Two layer norm layer and two droput layer
        self.layer_norm1 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)

        self.linear1 = nn.Linear(d_model, d_ff)
        self.activation = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(d_ff, d_model)

        self.layer_norm2 = nn.LayerNorm(d_model)
        self.dropout2 = nn.Dropout(dropout)

    def forward(self, query, key, values):
        """
        Input:
            block : object of type BasicBlock(nn.Module). It contains masked_attn_head objects which is of type MultiHeadAttention(nn.Module).
            mask : 0 means, it can peek only past values. 1 means, block can peek only current and pas values
            query : Query. In transformer paper it is the input for both encoder and decoder
            key : Keys. In transformer paper it is the input for both encoder and decoder
            Values. In transformer paper it is the input for encoder and  encoded output for decoder (in masked attention part)

        Output:
            query: Input gets changed over the layer and returned.

        """
        # 设置掩码为0，表示只能看到之前的信息
        mask = 0
        # 设置是否应用位置编码
        apply_pos = True
        # 获取序列长度和批次大小
        seqlen, batch_size = query.size(1), query.size(0)
        # 创建上三角矩阵掩码，用于限制注意力机制只能看到之前的信息
        nopeek_mask = np.triu(
            np.ones((1, 1, seqlen, seqlen)), k=mask).astype('uint8')
        # 将numpy数组转换为torch张量，并移至设备
        src_mask = (torch.from_numpy(nopeek_mask) == 0).to(device)

        # 根据掩码值决定是否进行零填充
        if mask == 0:  # 如果掩码为0，则需要进行零填充
            # 调用masked_attn_head的forward方法
            query2 = self.masked_attn_head(
                query, key, values, mask=src_mask, zero_pad=True)  # 只能看到之前的信息，当前的信息也看不到，此时会把第一行score全置0，表示第一道题看不到历史的interaction信息，第一题attn之后，对应value全0
        else:
            # 调用masked_attn_head的forward方法
            query2 = self.masked_attn_head(
                query, key, values, mask=src_mask, zero_pad=False)

        # 残差连接和Layer Normalization
        query = query + self.dropout1((query2))  # 残差1
        query = self.layer_norm1(query)  # layer norm

        # 如果需要应用位置编码
        if apply_pos:
            # 前馈神经网络
            query2 = self.linear2(self.dropout(  # FFN
                self.activation(self.linear1(query))))
            # 残差连接
            query = query + self.dropout2((query2))  # 残差
            # Layer Normalization
            query = self.layer_norm2(query)  # lay norm

        return query



class MultiHeadAttention(nn.Module):
    def __init__(self, d_model, d_feature, n_heads, dropout, kq_same, bias=True):
        super().__init__()
        """
        初始化函数
        它包含用于获取键、查询和值的投影层。后面跟着注意力机制和一个连接层。
        """
        self.d_model = d_model
        self.d_k = d_feature
        self.h = n_heads
        self.kq_same = kq_same

        # 值投影层
        self.v_linear = nn.Linear(d_model, d_model, bias=bias)
        # 键投影层
        self.k_linear = nn.Linear(d_model, d_model, bias=bias)
        # 如果键和查询不同
        if kq_same is False:
            # 查询投影层
            self.q_linear = nn.Linear(d_model, d_model, bias=bias)
        # Dropout层
        self.dropout = nn.Dropout(dropout)
        # 偏置标志
        self.proj_bias = bias
        # 输出投影层
        self.out_proj = nn.Linear(d_model, d_model, bias=bias)

        # 重置参数
        self._reset_parameters()


    def _reset_parameters(self):
        xavier_uniform_(self.k_linear.weight)
        xavier_uniform_(self.v_linear.weight)
        if self.kq_same is False:
            xavier_uniform_(self.q_linear.weight)

        if self.proj_bias:
            constant_(self.k_linear.bias, 0.)
            constant_(self.v_linear.bias, 0.)
            if self.kq_same is False:
                constant_(self.q_linear.bias, 0.)
            constant_(self.out_proj.bias, 0.)

    def forward(self, q, k, v, mask, zero_pad):
        # 获取批次大小
        bs = q.size(0)

        # 执行线性操作并将结果分割成多个头
        # perform linear operation and split into h heads

        # 对键进行线性操作并重新排列成多个头
        k = self.k_linear(k).view(bs, -1, self.h, self.d_k)
        # 如果键和查询不同
        if self.kq_same is False:
            # 对查询进行线性操作并重新排列成多个头
            q = self.q_linear(q).view(bs, -1, self.h, self.d_k)
        else:
            # 对查询进行线性操作并重新排列成多个头（使用键的线性层）
            q = self.k_linear(q).view(bs, -1, self.h, self.d_k)
        # 对值进行线性操作并重新排列成多个头
        v = self.v_linear(v).view(bs, -1, self.h, self.d_k)

        # 转置以获取维度 bs * h * sl * d_model
        # transpose to get dimensions bs * h * sl * d_model

        # 对键进行转置
        k = k.transpose(1, 2)
        # 对查询进行转置
        q = q.transpose(1, 2)
        # 对值进行转置
        v = v.transpose(1, 2)
        # 使用我们接下来将定义的函数计算注意力
        # calculate attention using function we will define next
        scores = attention(q, k, v, self.d_k,
                           mask, self.dropout, zero_pad)

        # 连接头并通过最终的线性层
        # concatenate heads and put through final linear layer
        concat = scores.transpose(1, 2).contiguous()\
            .view(bs, -1, self.d_model)

        # 通过最终的线性层
        output = self.out_proj(concat)

        return output



def attention(q, k, v, d_k, mask, dropout, zero_pad):
    """
    这是由多头注意力对象调用的，用于找到值。
    """
    # d_k: 每一个头的dim
    # 计算注意力分数
    scores = torch.matmul(q, k.transpose(-2, -1)) / \
        math.sqrt(d_k)  # BS, 8, seqlen, seqlen
    # 获取批次大小、头数和序列长度
    bs, head, seqlen = scores.size(0), scores.size(1), scores.size(2)

    # 对掩码为0的位置填充极小的值，表示这些位置不应被考虑
    scores.masked_fill_(mask == 0, -1e32)
    # 应用Softmax函数，将分数转换为概率分布
    scores = F.softmax(scores, dim=-1)  # BS,8,seqlen,seqlen
    # print(f"before zero pad scores: {scores.shape}")
    # print(zero_pad)
    # 如果需要，在第一行添加零填充
    if zero_pad:
        pad_zero = torch.zeros(bs, head, 1, seqlen).to(device)
        scores = torch.cat([pad_zero, scores[:, :, 1:, :]], dim=2) # 第一行score置0
    # print(f"after zero pad scores: {scores}")
    # 应用Dropout减少过拟合
    scores = dropout(scores)
    # 计算输出值
    output = torch.matmul(scores, v)
    # import sys
    # sys.exit()
    return output



class LearnablePositionalEmbedding(nn.Module):
    def __init__(self, d_model, max_len=512):
        super().__init__()
        # Compute the positional encodings once in log space.
        pe = 0.1 * torch.randn(max_len, d_model)
        pe = pe.unsqueeze(0)
        self.weight = nn.Parameter(pe, requires_grad=True)

    def forward(self, x):
        return self.weight[:, :x.size(Dim.seq), :]  # ( 1,seq,  Feature)


class CosinePositionalEmbedding(nn.Module):
    def __init__(self, d_model, max_len=512):
        super().__init__()
        # Compute the positional encodings once in log space.
        pe = 0.1 * torch.randn(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).long()
        div_term = torch.exp(torch.arange(0, d_model, 2).long() *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.weight = nn.Parameter(pe, requires_grad=False)

    def forward(self, x):
        return self.weight[:, :x.size(Dim.seq), :]  # ( 1,seq,  Feature)
