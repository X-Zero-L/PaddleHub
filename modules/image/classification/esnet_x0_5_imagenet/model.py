# copyright (c) 2021 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Tuple
from typing import Union

import paddle
import paddle.nn as nn
from paddle import concat
from paddle import ParamAttr
from paddle import reshape
from paddle import split
from paddle import transpose
from paddle.nn import AdaptiveAvgPool2D
from paddle.nn import BatchNorm
from paddle.nn import Conv2D
from paddle.nn import Dropout
from paddle.nn import Linear
from paddle.nn import MaxPool2D
from paddle.nn.initializer import KaimingNormal
from paddle.regularizer import L2Decay

MODEL_STAGES_PATTERN = {"ESNet": ["blocks[2]", "blocks[9]", "blocks[12]"]}


class Identity(nn.Layer):

    def __init__(self):
        super(Identity, self).__init__()

    def forward(self, inputs):
        return inputs


class TheseusLayer(nn.Layer):

    def __init__(self, *args, **kwargs):
        super(TheseusLayer, self).__init__()
        self.res_dict = {}
        self.res_name = self.full_name()
        self.pruner = None
        self.quanter = None

    def _return_dict_hook(self, layer, input, output):
        res_dict = {"output": output}
        # 'list' is needed to avoid error raised by popping self.res_dict
        for res_key in list(self.res_dict):
            # clear the res_dict because the forward process may change according to input
            res_dict[res_key] = self.res_dict.pop(res_key)
        return res_dict

    def init_res(self, stages_pattern, return_patterns=None, return_stages=None):
        if return_patterns and return_stages:
            msg = "The 'return_patterns' would be ignored when 'return_stages' is set."
            return_stages = None

        if return_stages is True:
            return_patterns = stages_pattern
        # return_stages is int or bool
        if type(return_stages) is int:
            return_stages = [return_stages]
        if isinstance(return_stages, list):
            if max(return_stages) > len(stages_pattern) or min(return_stages) < 0:
                msg = f"The 'return_stages' set error. Illegal value(s) have been ignored. The stages' pattern list is {stages_pattern}."
                return_stages = [val for val in return_stages if val >= 0 and val < len(stages_pattern)]
            return_patterns = [stages_pattern[i] for i in return_stages]

        if return_patterns:
            self.update_res(return_patterns)

    def replace_sub(self, *args, **kwargs) -> None:
        msg = "The function 'replace_sub()' is deprecated, please use 'upgrade_sublayer()' instead."
        raise DeprecationWarning(msg)

    def upgrade_sublayer(self, layer_name_pattern: Union[str, List[str]],
                         handle_func: Callable[[nn.Layer, str], nn.Layer]) -> Dict[str, nn.Layer]:
        """use 'handle_func' to modify the sub-layer(s) specified by 'layer_name_pattern'.

        Args:
            layer_name_pattern (Union[str, List[str]]): The name of layer to be modified by 'handle_func'.
            handle_func (Callable[[nn.Layer, str], nn.Layer]): The function to modify target layer specified by 'layer_name_pattern'. The formal params are the layer(nn.Layer) and pattern(str) that is (a member of) layer_name_pattern (when layer_name_pattern is List type). And the return is the layer processed.

        Returns:
            Dict[str, nn.Layer]: The key is the pattern and corresponding value is the result returned by 'handle_func()'.

        Examples:

            from paddle import nn
            import paddleclas

            def rep_func(layer: nn.Layer, pattern: str):
                new_layer = nn.Conv2D(
                    in_channels=layer._in_channels,
                    out_channels=layer._out_channels,
                    kernel_size=5,
                    padding=2
                )
                return new_layer

            net = paddleclas.MobileNetV1()
            res = net.replace_sub(layer_name_pattern=["blocks[11].depthwise_conv.conv", "blocks[12].depthwise_conv.conv"], handle_func=rep_func)
            print(res)
            # {'blocks[11].depthwise_conv.conv': the corresponding new_layer, 'blocks[12].depthwise_conv.conv': the corresponding new_layer}
        """

        if not isinstance(layer_name_pattern, list):
            layer_name_pattern = [layer_name_pattern]

        hit_layer_pattern_list = []
        for pattern in layer_name_pattern:
            # parse pattern to find target layer and its parent
            layer_list = parse_pattern_str(pattern=pattern, parent_layer=self)
            if not layer_list:
                continue
            sub_layer_parent = layer_list[-2]["layer"] if len(layer_list) > 1 else self

            sub_layer = layer_list[-1]["layer"]
            sub_layer_name = layer_list[-1]["name"]
            sub_layer_index = layer_list[-1]["index"]

            new_sub_layer = handle_func(sub_layer, pattern)

            if sub_layer_index:
                getattr(sub_layer_parent, sub_layer_name)[sub_layer_index] = new_sub_layer
            else:
                setattr(sub_layer_parent, sub_layer_name, new_sub_layer)

            hit_layer_pattern_list.append(pattern)
        return hit_layer_pattern_list

    def stop_after(self, stop_layer_name: str) -> bool:
        """stop forward and backward after 'stop_layer_name'.

        Args:
            stop_layer_name (str): The name of layer that stop forward and backward after this layer.

        Returns:
            bool: 'True' if successful, 'False' otherwise.
        """

        layer_list = parse_pattern_str(stop_layer_name, self)
        if not layer_list:
            return False

        parent_layer = self
        for layer_dict in layer_list:
            name, index = layer_dict["name"], layer_dict["index"]
            if not set_identity(parent_layer, name, index):
                msg = f"Failed to set the layers that after stop_layer_name('{stop_layer_name}') to IdentityLayer. The error layer's name is '{name}'."
                return False
            parent_layer = layer_dict["layer"]

        return True

    def update_res(self, return_patterns: Union[str, List[str]]) -> Dict[str, nn.Layer]:
        """update the result(s) to be returned.

        Args:
            return_patterns (Union[str, List[str]]): The name of layer to return output.

        Returns:
            Dict[str, nn.Layer]: The pattern(str) and corresponding layer(nn.Layer) that have been set successfully.
        """

        # clear res_dict that could have been set
        self.res_dict = {}

        class Handler(object):

            def __init__(self, res_dict):
                # res_dict is a reference
                self.res_dict = res_dict

            def __call__(self, layer, pattern):
                layer.res_dict = self.res_dict
                layer.res_name = pattern
                if hasattr(layer, "hook_remove_helper"):
                    layer.hook_remove_helper.remove()
                layer.hook_remove_helper = layer.register_forward_post_hook(save_sub_res_hook)
                return layer

        handle_func = Handler(self.res_dict)

        hit_layer_pattern_list = self.upgrade_sublayer(return_patterns, handle_func=handle_func)

        if hasattr(self, "hook_remove_helper"):
            self.hook_remove_helper.remove()
        self.hook_remove_helper = self.register_forward_post_hook(self._return_dict_hook)

        return hit_layer_pattern_list


def save_sub_res_hook(layer, input, output):
    layer.res_dict[layer.res_name] = output


def set_identity(parent_layer: nn.Layer, layer_name: str, layer_index: str = None) -> bool:
    """set the layer specified by layer_name and layer_index to Indentity.

    Args:
        parent_layer (nn.Layer): The parent layer of target layer specified by layer_name and layer_index.
        layer_name (str): The name of target layer to be set to Indentity.
        layer_index (str, optional): The index of target layer to be set to Indentity in parent_layer. Defaults to None.

    Returns:
        bool: True if successfully, False otherwise.
    """

    stop_after = False
    for sub_layer_name in parent_layer._sub_layers:
        if stop_after:
            parent_layer._sub_layers[sub_layer_name] = Identity()
            continue
        if sub_layer_name == layer_name:
            stop_after = True

    if layer_index and stop_after:
        stop_after = False
        for sub_layer_index in parent_layer._sub_layers[layer_name]._sub_layers:
            if stop_after:
                parent_layer._sub_layers[layer_name][sub_layer_index] = Identity()
                continue
            if layer_index == sub_layer_index:
                stop_after = True

    return stop_after


def parse_pattern_str(pattern: str, parent_layer: nn.Layer) -> Union[None, List[Dict[str, Union[nn.Layer, str, None]]]]:
    """parse the string type pattern.

    Args:
        pattern (str): The pattern to discribe layer.
        parent_layer (nn.Layer): The root layer relative to the pattern.

    Returns:
        Union[None, List[Dict[str, Union[nn.Layer, str, None]]]]: None if failed. If successfully, the members are layers parsed in order:
                                                                [
                                                                    {"layer": first layer, "name": first layer's name parsed, "index": first layer's index parsed if exist},
                                                                    {"layer": second layer, "name": second layer's name parsed, "index": second layer's index parsed if exist},
                                                                    ...
                                                                ]
    """

    pattern_list = pattern.split(".")
    if not pattern_list:
        msg = f"The pattern('{pattern}') is illegal. Please check and retry."
        return None

    layer_list = []
    while pattern_list:
        if '[' in pattern_list[0]:
            target_layer_name = pattern_list[0].split('[')[0]
            target_layer_index = pattern_list[0].split('[')[1].split(']')[0]
        else:
            target_layer_name = pattern_list[0]
            target_layer_index = None

        target_layer = getattr(parent_layer, target_layer_name, None)

        if target_layer is None:
            msg = f"Not found layer named('{target_layer_name}') specifed in pattern('{pattern}')."
            return None

        if target_layer_index and target_layer:
            if int(target_layer_index) < 0 or int(target_layer_index) >= len(target_layer):
                msg = f"Not found layer by index('{target_layer_index}') specifed in pattern('{pattern}'). The index should < {len(target_layer)} and > 0."
                return None

            target_layer = target_layer[target_layer_index]

        layer_list.append({"layer": target_layer, "name": target_layer_name, "index": target_layer_index})

        pattern_list = pattern_list[1:]
        parent_layer = target_layer
    return layer_list


def channel_shuffle(x, groups):
    batch_size, num_channels, height, width = x.shape[:4]
    channels_per_group = num_channels // groups
    x = reshape(x=x, shape=[batch_size, groups, channels_per_group, height, width])
    x = transpose(x=x, perm=[0, 2, 1, 3, 4])
    x = reshape(x=x, shape=[batch_size, num_channels, height, width])
    return x


def make_divisible(v, divisor=8, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class ConvBNLayer(TheseusLayer):

    def __init__(self, in_channels, out_channels, kernel_size, stride=1, groups=1, if_act=True):
        super().__init__()
        self.conv = Conv2D(in_channels=in_channels,
                           out_channels=out_channels,
                           kernel_size=kernel_size,
                           stride=stride,
                           padding=(kernel_size - 1) // 2,
                           groups=groups,
                           weight_attr=ParamAttr(initializer=KaimingNormal()),
                           bias_attr=False)

        self.bn = BatchNorm(out_channels,
                            param_attr=ParamAttr(regularizer=L2Decay(0.0)),
                            bias_attr=ParamAttr(regularizer=L2Decay(0.0)))
        self.if_act = if_act
        self.hardswish = nn.Hardswish()

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        if self.if_act:
            x = self.hardswish(x)
        return x


class SEModule(TheseusLayer):

    def __init__(self, channel, reduction=4):
        super().__init__()
        self.avg_pool = AdaptiveAvgPool2D(1)
        self.conv1 = Conv2D(in_channels=channel, out_channels=channel // reduction, kernel_size=1, stride=1, padding=0)
        self.relu = nn.ReLU()
        self.conv2 = Conv2D(in_channels=channel // reduction, out_channels=channel, kernel_size=1, stride=1, padding=0)
        self.hardsigmoid = nn.Hardsigmoid()

    def forward(self, x):
        identity = x
        x = self.avg_pool(x)
        x = self.conv1(x)
        x = self.relu(x)
        x = self.conv2(x)
        x = self.hardsigmoid(x)
        x = paddle.multiply(x=identity, y=x)
        return x


class ESBlock1(TheseusLayer):

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.pw_1_1 = ConvBNLayer(in_channels=in_channels // 2, out_channels=out_channels // 2, kernel_size=1, stride=1)
        self.dw_1 = ConvBNLayer(in_channels=out_channels // 2,
                                out_channels=out_channels // 2,
                                kernel_size=3,
                                stride=1,
                                groups=out_channels // 2,
                                if_act=False)
        self.se = SEModule(out_channels)

        self.pw_1_2 = ConvBNLayer(in_channels=out_channels, out_channels=out_channels // 2, kernel_size=1, stride=1)

    def forward(self, x):
        x1, x2 = split(x, num_or_sections=[x.shape[1] // 2, x.shape[1] // 2], axis=1)
        x2 = self.pw_1_1(x2)
        x3 = self.dw_1(x2)
        x3 = concat([x2, x3], axis=1)
        x3 = self.se(x3)
        x3 = self.pw_1_2(x3)
        x = concat([x1, x3], axis=1)
        return channel_shuffle(x, 2)


class ESBlock2(TheseusLayer):

    def __init__(self, in_channels, out_channels):
        super().__init__()

        # branch1
        self.dw_1 = ConvBNLayer(in_channels=in_channels,
                                out_channels=in_channels,
                                kernel_size=3,
                                stride=2,
                                groups=in_channels,
                                if_act=False)
        self.pw_1 = ConvBNLayer(in_channels=in_channels, out_channels=out_channels // 2, kernel_size=1, stride=1)
        # branch2
        self.pw_2_1 = ConvBNLayer(in_channels=in_channels, out_channels=out_channels // 2, kernel_size=1)
        self.dw_2 = ConvBNLayer(in_channels=out_channels // 2,
                                out_channels=out_channels // 2,
                                kernel_size=3,
                                stride=2,
                                groups=out_channels // 2,
                                if_act=False)
        self.se = SEModule(out_channels // 2)
        self.pw_2_2 = ConvBNLayer(in_channels=out_channels // 2, out_channels=out_channels // 2, kernel_size=1)
        self.concat_dw = ConvBNLayer(in_channels=out_channels,
                                     out_channels=out_channels,
                                     kernel_size=3,
                                     groups=out_channels)
        self.concat_pw = ConvBNLayer(in_channels=out_channels, out_channels=out_channels, kernel_size=1)

    def forward(self, x):
        x1 = self.dw_1(x)
        x1 = self.pw_1(x1)
        x2 = self.pw_2_1(x)
        x2 = self.dw_2(x2)
        x2 = self.se(x2)
        x2 = self.pw_2_2(x2)
        x = concat([x1, x2], axis=1)
        x = self.concat_dw(x)
        x = self.concat_pw(x)
        return x


class ESNet(TheseusLayer):

    def __init__(self,
                 stages_pattern,
                 class_num=1000,
                 scale=1.0,
                 dropout_prob=0.2,
                 class_expand=1280,
                 return_patterns=None,
                 return_stages=None):
        super().__init__()
        self.scale = scale
        self.class_num = class_num
        self.class_expand = class_expand
        stage_repeats = [3, 7, 3]
        stage_out_channels = [
            -1, 24, make_divisible(116 * scale),
            make_divisible(232 * scale),
            make_divisible(464 * scale), 1024
        ]

        self.conv1 = ConvBNLayer(in_channels=3, out_channels=stage_out_channels[1], kernel_size=3, stride=2)
        self.max_pool = MaxPool2D(kernel_size=3, stride=2, padding=1)

        block_list = []
        for stage_id, num_repeat in enumerate(stage_repeats):
            for i in range(num_repeat):
                if i == 0:
                    block = ESBlock2(in_channels=stage_out_channels[stage_id + 1],
                                     out_channels=stage_out_channels[stage_id + 2])
                else:
                    block = ESBlock1(in_channels=stage_out_channels[stage_id + 2],
                                     out_channels=stage_out_channels[stage_id + 2])
                block_list.append(block)
        self.blocks = nn.Sequential(*block_list)

        self.conv2 = ConvBNLayer(in_channels=stage_out_channels[-2], out_channels=stage_out_channels[-1], kernel_size=1)

        self.avg_pool = AdaptiveAvgPool2D(1)

        self.last_conv = Conv2D(in_channels=stage_out_channels[-1],
                                out_channels=self.class_expand,
                                kernel_size=1,
                                stride=1,
                                padding=0,
                                bias_attr=False)
        self.hardswish = nn.Hardswish()
        self.dropout = Dropout(p=dropout_prob, mode="downscale_in_infer")
        self.flatten = nn.Flatten(start_axis=1, stop_axis=-1)
        self.fc = Linear(self.class_expand, self.class_num)

        super().init_res(stages_pattern, return_patterns=return_patterns, return_stages=return_stages)

    def forward(self, x):
        x = self.conv1(x)
        x = self.max_pool(x)
        x = self.blocks(x)
        x = self.conv2(x)
        x = self.avg_pool(x)
        x = self.last_conv(x)
        x = self.hardswish(x)
        x = self.dropout(x)
        x = self.flatten(x)
        x = self.fc(x)
        return x


def ESNet_x0_5(pretrained=False, use_ssld=False, **kwargs):
    """
    ESNet_x0_5
    Args:
        pretrained: bool=False or str. If `True` load pretrained parameters, `False` otherwise.
                    If str, means the path of the pretrained model.
        use_ssld: bool=False. Whether using distillation pretrained model when pretrained=True.
    Returns:
        model: nn.Layer. Specific `ESNet_x0_5` model depends on args.
    """
    return ESNet(scale=0.5, stages_pattern=MODEL_STAGES_PATTERN["ESNet"], **kwargs)
