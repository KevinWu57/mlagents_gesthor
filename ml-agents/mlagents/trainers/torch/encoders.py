from typing import Tuple, Optional, Union

from mlagents.trainers.torch.layers import linear_layer, Initialization, Swish

from mlagents.torch_utils import torch, nn
from mlagents.trainers.torch.model_serialization import exporting_to_onnx

from torchvision import transforms, models


class Normalizer(nn.Module):
    def __init__(self, vec_obs_size: int):
        super().__init__()
        self.register_buffer("normalization_steps", torch.tensor(1))
        self.register_buffer("running_mean", torch.zeros(vec_obs_size))
        self.register_buffer("running_variance", torch.ones(vec_obs_size))

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        normalized_state = torch.clamp(
            (inputs - self.running_mean)
            / torch.sqrt(self.running_variance / self.normalization_steps),
            -5,
            5,
        )
        return normalized_state

    def update(self, vector_input: torch.Tensor) -> None:
        steps_increment = vector_input.size()[0]
        total_new_steps = self.normalization_steps + steps_increment

        input_to_old_mean = vector_input - self.running_mean
        new_mean = self.running_mean + (input_to_old_mean / total_new_steps).sum(0)

        input_to_new_mean = vector_input - new_mean
        new_variance = self.running_variance + (
            input_to_new_mean * input_to_old_mean
        ).sum(0)
        # Update in-place
        self.running_mean.data.copy_(new_mean.data)
        self.running_variance.data.copy_(new_variance.data)
        self.normalization_steps.data.copy_(total_new_steps.data)

    def copy_from(self, other_normalizer: "Normalizer") -> None:
        self.normalization_steps.data.copy_(other_normalizer.normalization_steps.data)
        self.running_mean.data.copy_(other_normalizer.running_mean.data)
        self.running_variance.copy_(other_normalizer.running_variance.data)


def conv_output_shape(
    h_w: Tuple[int, int],
    kernel_size: Union[int, Tuple[int, int]] = 1,
    stride: int = 1,
    padding: int = 0,
    dilation: int = 1,
) -> Tuple[int, int]:
    """
    Calculates the output shape (height and width) of the output of a convolution layer.
    kernel_size, stride, padding and dilation correspond to the inputs of the
    torch.nn.Conv2d layer (https://pytorch.org/docs/stable/generated/torch.nn.Conv2d.html)
    :param h_w: The height and width of the input.
    :param kernel_size: The size of the kernel of the convolution (can be an int or a
    tuple [width, height])
    :param stride: The stride of the convolution
    :param padding: The padding of the convolution
    :param dilation: The dilation of the convolution
    """
    from math import floor

    if not isinstance(kernel_size, tuple):
        kernel_size = (int(kernel_size), int(kernel_size))
    h = floor(
        ((h_w[0] + (2 * padding) - (dilation * (kernel_size[0] - 1)) - 1) / stride) + 1
    )
    w = floor(
        ((h_w[1] + (2 * padding) - (dilation * (kernel_size[1] - 1)) - 1) / stride) + 1
    )
    return h, w


def pool_out_shape(h_w: Tuple[int, int], kernel_size: int) -> Tuple[int, int]:
    """
    Calculates the output shape (height and width) of the output of a max pooling layer.
    kernel_size corresponds to the inputs of the
    torch.nn.MaxPool2d layer (https://pytorch.org/docs/stable/generated/torch.nn.MaxPool2d.html)
    :param kernel_size: The size of the kernel of the convolution
    """
    height = (h_w[0] - kernel_size) // 2 + 1
    width = (h_w[1] - kernel_size) // 2 + 1
    return height, width


class VectorInput(nn.Module):
    def __init__(self, input_size: int, normalize: bool = False):
        super().__init__()
        self.normalizer: Optional[Normalizer] = None
        if normalize:
            self.normalizer = Normalizer(input_size)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        if self.normalizer is not None:
            inputs = self.normalizer(inputs)
        return inputs

    def copy_normalization(self, other_input: "VectorInput") -> None:
        if self.normalizer is not None and other_input.normalizer is not None:
            self.normalizer.copy_from(other_input.normalizer)

    def update_normalization(self, inputs: torch.Tensor) -> None:
        if self.normalizer is not None:
            self.normalizer.update(inputs)


class SmallVisualEncoder(nn.Module):
    """
    CNN architecture used by King in their Candy Crush predictor
    https://www.researchgate.net/publication/328307928_Human-Like_Playtesting_with_Deep_Learning
    """

    def __init__(
        self, height: int, width: int, initial_channels: int, output_size: int
    ):
        super().__init__()
        self.h_size = output_size
        conv_1_hw = conv_output_shape((height, width), 3, 1)
        conv_2_hw = conv_output_shape(conv_1_hw, 3, 1)
        self.final_flat = conv_2_hw[0] * conv_2_hw[1] * 144

        self.conv_layers = nn.Sequential(
            nn.Conv2d(initial_channels, 35, [3, 3], [1, 1]),
            nn.LeakyReLU(),
            nn.Conv2d(35, 144, [3, 3], [1, 1]),
            nn.LeakyReLU(),
        )
        self.dense = nn.Sequential(
            linear_layer(
                self.final_flat,
                self.h_size,
                kernel_init=Initialization.KaimingHeNormal,
                kernel_gain=1.41,  # Use ReLU gain
            ),
            nn.LeakyReLU(),
        )

    def forward(self, visual_obs: torch.Tensor) -> torch.Tensor:
        if not exporting_to_onnx.is_exporting():
            visual_obs = visual_obs.permute([0, 3, 1, 2])
        hidden = self.conv_layers(visual_obs)
        hidden = hidden.reshape(-1, self.final_flat)
        return self.dense(hidden)

class SimpleVisualEncoder(nn.Module):
    def __init__(
        self, height: int, width: int, initial_channels: int, output_size: int
    ):
        super().__init__()
        self.h_size = output_size
        conv_1_hw = conv_output_shape((height, width), 8, 4)
        maxpool_1_hw = conv_output_shape(conv_1_hw, 2, 2)
        conv_2_hw = conv_output_shape(maxpool_1_hw, 4, 2)
        maxpool_2_hw = conv_output_shape(conv_2_hw, 2, 2)
        self.final_flat = maxpool_2_hw[0] * maxpool_2_hw[1] * 32 # the final flatten size of the neural net

        self.conv_layers = nn.Sequential(
            nn.Conv2d(initial_channels, 16, [8, 8], [4, 4]),
            nn.LeakyReLU(),
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(16, 32, [4, 4], [2, 2]),
            nn.LeakyReLU(),
            nn.MaxPool2d(kernel_size=2),
        )
        self.dense = nn.Sequential(
            # linear_layer(
            #     self.final_flat,
            #     self.h_size,
            #     kernel_init=Initialization.KaimingHeNormal,
            #     kernel_gain=1.41,  # Use ReLU gain
            # ),
            # nn.LeakyReLU(),
            nn.Linear(self.final_flat, self.h_size),
            nn.LeakyReLU(),
        )

    def forward(self, visual_obs: torch.Tensor) -> torch.Tensor:
        if not exporting_to_onnx.is_exporting():
            visual_obs = visual_obs.permute([0, 3, 1, 2])
        hidden = self.conv_layers(visual_obs)
        hidden = hidden.reshape(-1, self.final_flat)
        return self.dense(hidden)

class ResNet18VisualEncoder(nn.Module):
    """
    ResNet18 archetecture. Network pretrained on ImageNet and extract a feature vector from the network
    """
    def __init__(
        self, height: int, width: int, initial_channels: int, output_size: int
    ):
        super().__init__()
        self.channel = initial_channels
        self.h_size = output_size # this is the final output size
        conv_1_hw = conv_output_shape((height, width), 8, 4)
        maxpool_1_hw = conv_output_shape(conv_1_hw, 2, 2)
        conv_2_hw = conv_output_shape(maxpool_1_hw, 4, 2)
        maxpool_2_hw = conv_output_shape(conv_2_hw, 2, 2)
        self.final_flat = self.h_size if initial_channels == 3 else maxpool_2_hw[0] * maxpool_2_hw[1] * 32 # the final flatten size of the neural net

        # Load the pretrained MobileNet v2 model
        self.resnet18 = models.resnet18(pretrained=True)

        # Freeze all parameters in the model
        for param in self.resnet18.parameters():
            param.requires_grad = False

        for module in self.resnet18.modules():
            if isinstance(module, nn.BatchNorm2d):
                module.eval()
                module.momentum = 0 # TODO: not sure if this is correct

        # Change the last FC classifier
        self.resnet18.fc = nn.Sequential(
            nn.Linear(in_features=512, out_features=self.h_size, bias=True),
            nn.LeakyReLU(),
        ) 
        
        # Use multiple GPUs if possible
        if torch.cuda.device_count() > 1:
            self.resnet18 = nn.DataParallel(self.resnet18, device_ids=[1,3]) # TODO: use distributed dataparallel?

        self.conv_layers = nn.Sequential(
            nn.Conv2d(initial_channels, 16, [8, 8], [4, 4]),
            nn.LeakyReLU(),
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(16, 32, [4, 4], [2, 2]),
            nn.LeakyReLU(),
            nn.MaxPool2d(kernel_size=2),
        )
        self.dense = nn.Sequential(
            # linear_layer(
            #     self.final_flat,
            #     self.h_size,
            #     kernel_init=Initialization.KaimingHeNormal,
            #     kernel_gain= 1.41,  # Use ReLU gain
            # ),
            nn.Linear(self.final_flat, self.h_size),
            nn.LeakyReLU(),
        )

    def forward(self, visual_obs: torch.Tensor) -> torch.Tensor:
        if not exporting_to_onnx.is_exporting():
            visual_obs = visual_obs.permute([0, 3, 1, 2]) # permute the dimensions to match the input for conv_layers

        # hidden = self.conv_layers(visual_obs)

        # Only use MobileNet for color images (i.e., images with 3 channels)
        # Depth images will use the default simple network
        if self.channel == 3:
            # normalize the input tensor
            transform  = transforms.Normalize(mean = [0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            visual_obs = transform(visual_obs)
            hidden = self.resnet18(visual_obs)
            return hidden
        else:
            hidden = self.conv_layers(visual_obs)
            hidden = hidden.reshape(-1, self.final_flat)
            return self.dense(hidden)


class NatureVisualEncoder(nn.Module):
    def __init__(
        self, height: int, width: int, initial_channels: int, output_size: int
    ):
        super().__init__()
        self.h_size = output_size
        conv_1_hw = conv_output_shape((height, width), 8, 4)
        maxpool_1_hw = conv_output_shape(conv_1_hw, 2, 2)
        conv_2_hw = conv_output_shape(maxpool_1_hw, 4, 2)
        maxpool_2_hw = conv_output_shape(conv_2_hw, 2, 2)
        conv_3_hw = conv_output_shape(maxpool_2_hw, 3, 1)
        # maxpool_3_hw = conv_output_shape(conv_3_hw, 2, 2)
        self.final_flat = conv_3_hw[0] * conv_3_hw[1] * 64

        self.conv_layers = nn.Sequential(
            nn.Conv2d(initial_channels, 32, [8, 8], [4, 4]),
            nn.MaxPool2d(kernel_size=2),
            nn.LeakyReLU(),
            nn.Conv2d(32, 64, [4, 4], [2, 2]),
            nn.LeakyReLU(),
            nn.MaxPool2d(kernel_size=2),
            nn.Conv2d(64, 64, [3, 3], [1, 1]),
            nn.LeakyReLU(),
            # nn.MaxPool2d(kernel_size=2),
        )
        self.dense = nn.Sequential(
            # linear_layer(
            #     self.final_flat,
            #     self.h_size,
            #     kernel_init=Initialization.KaimingHeNormal,
            #     kernel_gain=1.41,  # Use ReLU gain
            # ),
            # nn.LeakyReLU(),
            nn.Linear(self.final_flat, self.h_size),
            nn.LeakyReLU(),
        )

    def forward(self, visual_obs: torch.Tensor) -> torch.Tensor:
        if not exporting_to_onnx.is_exporting():
            visual_obs = visual_obs.permute([0, 3, 1, 2])
        hidden = self.conv_layers(visual_obs)
        hidden = hidden.reshape([-1, self.final_flat])
        return self.dense(hidden)


class ResNetBlock(nn.Module):
    def __init__(self, channel: int):
        """
        Creates a ResNet Block.
        :param channel: The number of channels in the input (and output) tensors of the
        convolutions
        """
        super().__init__()
        self.layers = nn.Sequential(
            Swish(),
            nn.Conv2d(channel, channel, [3, 3], [1, 1], padding=1),
            Swish(),
            nn.Conv2d(channel, channel, [3, 3], [1, 1], padding=1),
        )

    def forward(self, input_tensor: torch.Tensor) -> torch.Tensor:
        return input_tensor + self.layers(input_tensor)


class ResNetVisualEncoder(nn.Module):
    def __init__(
        self, height: int, width: int, initial_channels: int, output_size: int
    ):
        super().__init__()
        n_channels = [16, 32, 32]  # channel for each stack
        n_blocks = 2  # number of residual blocks
        layers = []
        last_channel = initial_channels
        for _, channel in enumerate(n_channels):
            layers.append(nn.Conv2d(last_channel, channel, [3, 3], [1, 1], padding=1))
            layers.append(nn.MaxPool2d([3, 3], [2, 2]))
            height, width = pool_out_shape((height, width), 3)
            for _ in range(n_blocks):
                layers.append(ResNetBlock(channel))
            last_channel = channel
        layers.append(Swish())
        self.dense = linear_layer(
            n_channels[-1] * height * width,
            output_size,
            kernel_init=Initialization.KaimingHeNormal,
            kernel_gain=1.41,  # Use ReLU gain
        )
        self.sequential = nn.Sequential(*layers)

    def forward(self, visual_obs: torch.Tensor) -> torch.Tensor:
        if not exporting_to_onnx.is_exporting():
            visual_obs = visual_obs.permute([0, 3, 1, 2])
        batch_size = visual_obs.shape[0]
        hidden = self.sequential(visual_obs)
        before_out = hidden.reshape(batch_size, -1)
        return torch.relu(self.dense(before_out))