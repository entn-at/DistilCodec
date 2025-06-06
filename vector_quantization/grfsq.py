from dataclasses import dataclass

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from vector_quantize_pytorch import GroupedResidualFSQ, GroupedResidualVQ

from models import ConvNeXtBlock


@dataclass
class FSQResult:
    quantized: torch.Tensor
    codes: torch.Tensor
    latents: torch.Tensor


class DownsampleGRFSQ(nn.Module):
    def __init__(
        self,
        input_dim: int = 512,
        n_codebooks: int = 1,
        n_groups: int = 1,
        levels: tuple[int] = [8, 5, 5, 5],  # Approximate 2**10
        downsample_factor: tuple[int] = (2, 2),
        downsample_dims: tuple[int] | None = None,
    ):
        super().__init__()

        if downsample_dims is None:
            downsample_dims = [input_dim for i, _ in enumerate(range(len(downsample_factor)))]

        all_dims = (input_dim,) + tuple(downsample_dims)

        self.residual_fsq = GroupedResidualFSQ(
            dim=all_dims[-1],
            levels=levels,
            num_quantizers=n_codebooks,
            groups=n_groups,
        )

        self.downsample_factor = downsample_factor
        self.downsample_dims = downsample_dims

        self.downsample = nn.Sequential(
            *[
                nn.Sequential(
                    nn.Conv1d(
                        all_dims[idx],
                        all_dims[idx + 1],
                        kernel_size=factor,
                        stride=factor,
                    ),
                    ConvNeXtBlock(dim=all_dims[idx + 1]),
                )
                for idx, factor in enumerate(downsample_factor)
            ]
        )

        self.upsample = nn.Sequential(
            *[
                nn.Sequential(
                    nn.ConvTranspose1d(
                        all_dims[idx + 1],
                        all_dims[idx],
                        kernel_size=factor,
                        stride=factor,
                    ),
                    ConvNeXtBlock(dim=all_dims[idx]),
                )
                for idx, factor in reversed(list(enumerate(downsample_factor)))
            ]
        )

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, (nn.Conv1d, nn.Linear)):
            nn.init.trunc_normal_(m.weight, std=0.02)
            nn.init.constant_(m.bias, 0)

    def forward(self, z) -> FSQResult:
        original_shape = z.shape
        z = self.downsample(z)
        quantized, indices = self.residual_fsq(z.mT)
        result = FSQResult(
            quantized=quantized.mT,
            codes=indices.mT,
            latents=z,
        )
        result.quantized = self.upsample(result.quantized)

        # Pad or crop z to match original shape
        diff = original_shape[-1] - result.quantized.shape[-1]
        left = diff // 2
        right = diff - left

        if diff > 0:
            result.quantized = F.pad(result.quantized, (left, right))
        elif diff < 0:
            result.quantized = result.quantized[..., left:-right]

        return result

    def encode(self, z):
        print(z.shape)
        z = self.downsample(z)
        print(z.shape)
        _, indices = self.residual_fsq(z.mT)
        indices = rearrange(indices, "g b l r -> b (g r) l")
        return indices

    def decode(self, indices: torch.Tensor):
        indices = rearrange(indices, "b (g r) l -> g b l r", g=self.residual_fsq.groups)
        z_q = self.residual_fsq.get_output_from_indices(indices)
        z_q = self.upsample(z_q.mT)
        return z_q

    # def from_latents(self, latents: torch.Tensor):
    #     z_q, z_p, codes = super().from_latents(latents)
    #     z_q = self.upsample(z_q)
    #     return z_q, z_p, codes


if __name__ == "__main__":
    rvq = DownsampleGRFSQ(
        n_codebooks=1,
        downsample_factor=(2, 2),
    )
    x = torch.randn(16, 512, 80)

    result = rvq(x)
    print(rvq)
    print(result.latents.shape, result.codes.shape, result.z.shape)

    # y = rvq.from_codes(result.codes)
    # print(y[0].shape)

    # y = rvq.from_latents(result.latents)
    # print(y[0].shape)
