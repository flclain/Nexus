# code from https://github.com/esceptico/perceiver-io/
from typing import Optional

import torch
import torch.nn as nn
from nuplan.planning.training.modeling.types import TargetsType

from nuplan_extent.planning.training.preprocessing.feature_builders.nexus_feature_builder import (
    SceneTensor,
)

import os

class LoRALayer(nn.Module):
    def __init__(self, linear_layer, use_lora: bool = False, rank=8):
        """
        Wraps a linear layer and adds LoRA (Low-Rank Adaptation).

        Args:
            linear_layer (nn.Linear): The linear layer to be wrapped.
            rank (int): Rank of the low-rank decomposition. Default is 8.
        """
        super(LoRALayer, self).__init__()

        # self.use_lora = use_lora
        # if os.environ.get('USE_LoRA', 'False').lower() == 'true':
        #     self.use_lora = True

        # Get the shape of the original weight matrix
        self.in_features = linear_layer.in_features
        self.out_features = linear_layer.out_features
        # Initialize LoRA matrices A and B
        self.lora_A = nn.Parameter(torch.zeros(self.in_features, rank))  # LoRA matrix A
        self.lora_B = nn.Parameter(torch.zeros(rank, self.out_features))  # LoRA matrix B

        # Apply Kaiming initialization to LoRA matrix A
        nn.init.kaiming_normal_(self.lora_A, mode='fan_in', nonlinearity='relu')

    def forward(self, x):
        """
        Forward pass.

        Args:
            x (torch.Tensor): Input tensor.

        Returns:
            torch.Tensor: Output tensor.
        """
        # if self.use_lora:
        #     # LoRA output: x @ A @ B
        #     lora_output = x @ self.lora_A @ self.lora_B
        # else:
        #     lora_output = 0.
        lora_output = x @ self.lora_A @ self.lora_B
        return lora_output


class MultiHeadAttention(nn.Module):
    """Multi-head attention"""

    def __init__(
        self,
        kv_dim: int,
        q_dim: int,
        *,
        qk_out_dim: Optional[int] = None,
        v_out_dim: Optional[int] = None,
        output_dim: Optional[int] = None,
        num_heads: int = 1,
        dropout: float = 0.0,
    ):
        """Constructor.

        Args:
            kv_dim: Size of input key and value vectors.
            q_dim: Size of input query vector.
            qk_out_dim: Size of Query and Key matrices last dimension.
                If None, it will be equal to q_dim. Defaults to None.
            v_out_dim: Size of Value matrix last dimension.
                If None, it will be equal to qk_out_dim. Defaults to None.
            output_dim: Size of output after the QKV attention.
                If none, it will be equal to v_out_dim. Defaults to None.
            num_heads: Number of heads. Defaults to 1.
            dropout: Dropout probability. Defaults to 0.0.
        """
        super().__init__()

        if qk_out_dim is None:
            qk_out_dim = q_dim
        if v_out_dim is None:
            v_out_dim = qk_out_dim
        if output_dim is None:
            output_dim = v_out_dim

        self.num_heads = num_heads
        self.qk_head_dim = qk_out_dim // num_heads
        self.v_head_dim = v_out_dim // num_heads

        self.q = nn.Linear(q_dim, qk_out_dim)  
        self.v = nn.Linear(kv_dim, v_out_dim) 
        
        self.use_lora = os.environ.get('USE_LoRA', 'False').lower() == 'true'
        # lora layer
        if self.use_lora:
            self.q_lora = LoRALayer(self.q)
            self.v_lora = LoRALayer(self.v)

        self.k = nn.Linear(kv_dim, qk_out_dim)
        self.projection = nn.Linear(v_out_dim, output_dim)
        self.dropout = nn.Dropout(dropout)
        self.scale = self.qk_head_dim**-0.5

    def transform_for_scores(self, x: torch.Tensor, head_dim: int):
        # (..., seq_len, dim) -> (..., n_heads, seq_len, head_dim)
        *dims, seq, hid = x.size()
        x = x.view(*dims, seq, self.num_heads, head_dim)
        return x.transpose(-3, -2)

    def forward(
        self,
        inputs_kv: torch.Tensor,
        inputs_q: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
    ):
        """
        Args:
            inputs_kv: Key/Value embeddings of shape (B, ..., M, C).
            inputs_q: Query embeddings of shape (B, ..., N, D)
            attention_mask: Tensor of shape (B, ..., N, M).

        Returns:
            Tensor of shape (B, ..., N, D)
        """
        keys, queries, values = self.k(inputs_kv), self.q(inputs_q), self.v(inputs_kv)
        # lora layer
        if self.use_lora:
            queries += self.q_lora(inputs_q)
            values += self.v_lora(inputs_kv)
        
        keys = self.transform_for_scores(keys, self.qk_head_dim)
        queries = self.transform_for_scores(queries, self.qk_head_dim)
        values = self.transform_for_scores(values, self.v_head_dim)
        attention = queries @ keys.transpose(-2, -1) * self.scale
        if attention_mask is not None:
            min_value = torch.finfo(attention.dtype).min
            extended_mask = ~attention_mask * min_value
            attention = attention + extended_mask
        attention = attention.softmax(dim=-1)
        attention = self.dropout(attention)
        if attention_mask is not None:
            attention = attention.masked_fill(~attention_mask, value=0)
        weighted = attention @ values
        # (..., n_heads, seq_len, head_dim) -> (..., seq_len, hid)
        *dims, n_heads, seq, hid = weighted.size()
        weighted = weighted.transpose(-3, -2)
        weighted = weighted.reshape(*dims, seq, n_heads * hid)
        return self.projection(weighted)


class FeedForward(nn.Module):
    """Transformer Feed-Forward network."""

    def __init__(self, dim: int, widening_factor: int = 4, dropout: float = 0.0):
        """Constructor.

        Args:
            dim: Dimension of input tensor.
            widening_factor: Widening factor. Defaults to 4.
            dropout: Dropout probability. Defaults to 0.
        """
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim * widening_factor),
            nn.GELU(),
            nn.Linear(dim * widening_factor, dim),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor):
        return self.mlp(x)


class SelfAttention(nn.Module):
    """Self-attention module."""

    def __init__(
        self,
        *,
        hidden_dim: int,
        qk_out_dim: Optional[int] = None,
        v_out_dim: Optional[int] = None,
        widening_factor: int = 4,
        num_heads: int = 1,
        dropout: float = 0.0,
        attention_dropout: float = 0.0,
    ):
        """Constructor.

        Args:
            hidden_dim: Dimension of input tensor.
            qk_out_dim: Size of Query and Key matrices last dimension.
                Defaults to None.
            v_out_dim: Size of Value matrix last dimension.
                Defaults to None.
            widening_factor: Feed-forward network widening factor.
                Defaults to 4.
            num_heads: Number of attention heads. Defaults to 1.
            dropout: Dropout probability. Defaults to 0.
            attention_dropout: Attention scores probability. Defaults to 0.
        """
        super().__init__()
        self.layer_norm = nn.LayerNorm(hidden_dim)
        self.qkv_layer_norm = nn.LayerNorm(hidden_dim)
        self.attention = MultiHeadAttention(
            kv_dim=hidden_dim,
            q_dim=hidden_dim,
            qk_out_dim=qk_out_dim,
            v_out_dim=v_out_dim,
            output_dim=hidden_dim,
            num_heads=num_heads,
            dropout=attention_dropout,
        )
        self.dropout = nn.Dropout(dropout)
        self.mlp = FeedForward(hidden_dim, widening_factor, dropout)

    def forward(self, x: torch.Tensor, attention_mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: Input tensor of shape (B, ..., M, C).
            attention_mask: Input mask tensor of shape (B, ..., M, M).
                Mask values selected in [0, 1]. Defaults to None.
        """
        x_norm = self.layer_norm(x)
        attention = self.attention(
            inputs_kv=x_norm, inputs_q=x_norm, attention_mask=attention_mask
        )
        attention = self.dropout(attention)
        x = x + attention
        x = x + self.mlp(self.qkv_layer_norm(x))
        return x


class CrossAttention(nn.Module):
    """Cross-attention module."""

    def __init__(
        self,
        *,
        kv_dim: int,
        q_dim: int,
        qk_out_dim: Optional[int] = None,
        v_out_dim: Optional[int] = None,
        widening_factor: int = 1,
        num_heads: int = 1,
        use_query_residual: bool = True,
        dropout: float = 0.0,
        attention_dropout: float = 0.0,
    ):
        """Constructor.

        Args:
            kv_dim: Dimension of key/value input tensor.
            q_dim: Dimension of query input tensor.
            qk_out_dim: Size of Query and Key matrices last dimension.
                Defaults to None.
            v_out_dim: Size of Value matrix last dimension.
                Defaults to None.
            widening_factor: Feed-forward network widening factor.
                Defaults to 4.
            num_heads: Number of attention heads. Defaults to 1.
            use_query_residual: Indicates whether to use query residual in
                cross-attention. Defaults to True.
            dropout: Dropout probability. Defaults to 0.
            attention_dropout: Attention scores probability. Defaults to 0.
        """
        super().__init__()
        self.use_query_residual = use_query_residual
        self.kv_layer_norm = nn.LayerNorm(kv_dim)
        self.q_layer_norm = nn.LayerNorm(q_dim)
        self.qkv_layer_norm = nn.LayerNorm(q_dim)
        self.attention = MultiHeadAttention(
            kv_dim=kv_dim,
            q_dim=q_dim,
            qk_out_dim=qk_out_dim,
            v_out_dim=v_out_dim,
            output_dim=q_dim,
            num_heads=num_heads,
            dropout=attention_dropout,
        )
        self.dropout = nn.Dropout(dropout)
        self.mlp = FeedForward(q_dim, widening_factor, dropout)

    def forward(
        self,
        inputs_kv: torch.Tensor,
        inputs_q: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
    ):
        """
        Args:
            inputs_kv: Key/Value embeddings of shape (B, ..., M, C).
            inputs_q: Query embeddings of shape (B, ..., N, D)
            attention_mask: Tensor of shape (B, ..., N, M). Mask values selected
                in [0, 1]. Defaults to None.
        """
        attention = self.attention(
            inputs_kv=self.kv_layer_norm(inputs_kv),
            inputs_q=self.q_layer_norm(inputs_q),
            attention_mask=attention_mask,
        )
        attention = self.dropout(attention)
        if self.use_query_residual:
            x = inputs_q + attention
        else:
            x = attention
        x = x + self.mlp(self.qkv_layer_norm(x))
        return x


class Encoder(nn.Module):
    """Perceiver encoder module. Consists of two components: cross-attention
    module that maps an input tensor and a trainable latent tensor to a latent
    tensor and a stacked Transformer blocks with shared weights.
    """

    def __init__(
        self,
        num_latents: int,
        latent_dim: int,
        input_dim: int,
        num_self_attn_per_block: int = 2,
        num_blocks: int = 4,
        qk_out_dim: Optional[int] = None,
        v_out_dim: Optional[int] = None,
        num_cross_attn_heads: int = 1,
        num_self_attn_heads: int = 8,
        cross_attn_widening_factor: int = 1,
        self_attn_widening_factor: int = 1,
        use_query_residual: bool = True,
        dropout: float = 0.0,
        cross_attention_dropout: float = 0.0,
        self_attention_dropout: float = 0.0,
    ):
        """Constructor.

        Args:
            num_latents: Number of latent vectors.
            latent_dim: Dimension of latent vector.
            input_dim: Dimension of input tensor.
            num_self_attn_per_block: Number of self-attention modules per
                transformer block. Defaults to 2.
            num_blocks: Number of transformer blocks. Defaults to 4.
            qk_out_dim: Size of Query and Key matrices last dimension.
                Defaults to None.
            v_out_dim: Size of Value matrix last dimension.
                Defaults to None.
            num_cross_attn_heads: Number of cross-attention heads.
                Defaults to 1.
            num_self_attn_heads: Number of self-attention heads.
                Defaults to 8.
            cross_attn_widening_factor: Widening factor in cross-attention
                feed-forward layer. Defaults to 1.
            self_attn_widening_factor: Widening factor in self-attention
                feed-forward layer. Defaults to 1.
            use_query_residual: Indicates whether to use query residual in
                cross-attention. Defaults to True.
            dropout: Feed-forward dropout probability. Defaults to 0.
            cross_attention_dropout: Cross-attention scores dropout probability.
                Defaults to 0.
            self_attention_dropout: Self-attention scores dropout probability.
                Defaults to 0.
        """
        super().__init__()
        self.num_blocks = num_blocks

        self.latents = nn.Parameter(torch.randn(num_latents, latent_dim))
        self.cross_attn = CrossAttention(
            kv_dim=input_dim,
            q_dim=latent_dim,
            widening_factor=cross_attn_widening_factor,
            num_heads=num_cross_attn_heads,
            qk_out_dim=qk_out_dim,
            v_out_dim=v_out_dim,
            use_query_residual=use_query_residual,
            dropout=dropout,
            attention_dropout=cross_attention_dropout,
        )
        self.self_attention_block = nn.ModuleList(
            [
                SelfAttention(
                    hidden_dim=latent_dim,
                    widening_factor=self_attn_widening_factor,
                    num_heads=num_self_attn_heads,
                    qk_out_dim=qk_out_dim,
                    v_out_dim=v_out_dim,
                    dropout=dropout,
                    attention_dropout=self_attention_dropout,
                )
                for _ in range(num_self_attn_per_block)
            ]
        )

    def forward(self, x: torch.Tensor, kv_mask: Optional[torch.Tensor] = None):
        """
        Args:
            x: Input tensor of shape (B, M, C).
            kv_mask: Input mask tensor of shape (B, M). Mask values selected
                in [0, 1]. Defaults to None.

        Returns:
            Latent tensor.
        """
        batch_size = x.size(0)
        if kv_mask is not None:
            kv_mask = kv_mask[:, None, None, :]

        latents = self.cross_attn(
            inputs_kv=x,
            inputs_q=self.latents.repeat(batch_size, 1, 1),
            attention_mask=kv_mask,
        )
        for _ in range(self.num_blocks):
            for self_attn_layer in self.self_attention_block:
                latents = self_attn_layer(latents)
        return latents


class PercieverEncoder(nn.Module):
    """Wrapper around the perciever io encoder"""

    def __init__(
        self,
        num_latents: int,
        latent_dim: int,
        input_dim: int,
        num_self_attn_per_block: int = 2,
        num_blocks: int = 4,
        qk_out_dim: Optional[int] = None,
        v_out_dim: Optional[int] = None,
        num_cross_attn_heads: int = 1,
        num_self_attn_heads: int = 8,
        cross_attn_widening_factor: int = 1,
        self_attn_widening_factor: int = 1,
        use_query_residual: bool = True,
        dropout: float = 0.0,
        cross_attention_dropout: float = 0.0,
        self_attention_dropout: float = 0.0,
    ):
        super().__init__()
        self._encoder = Encoder(
            num_latents,
            latent_dim,
            input_dim,
            num_self_attn_per_block,
            num_blocks,
            qk_out_dim,
            v_out_dim,
            num_cross_attn_heads,
            num_self_attn_heads,
            cross_attn_widening_factor,
            self_attn_widening_factor,
            use_query_residual,
            dropout,
            cross_attention_dropout,
            self_attention_dropout,
        )

    def forward(self, scene_tensor: SceneTensor) -> TargetsType:
        """
        Predict
        :param features: input features containing rasterized data.
            features['raster'] is a HorizonRasterV2 object or a dictionary of
            raster layer tensors.
        :return: encoder_features: dict of predictions from network
        """
        rg: torch.Tensor = scene_tensor.road_graph  # B x n_lines x n_points x n_dim
        rgv: torch.Tensor = scene_tensor.road_graph_validity
        rg_invalid = (rgv == 0.0).all(dim=-1)  # B x n_lines x n_points
        B, NL, NP, ND = rg.shape

        rg = rg.view(B, NL * NP, ND)
        rg_invalid = rg_invalid.view(B, NL * NP)

        latents = self._encoder.forward(
            x=rg, kv_mask=rg_invalid
        )  # B x n_latents x latent dim

        return latents
