# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math
import re
import warnings
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Union
from einops import rearrange

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.pytorch_utils import Conv1D

from ..import_utils import is_bnb_4bit_available, is_bnb_available
from ..utils import (
    COMMON_LAYERS_PATTERN,
    TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING,
    ModulesToSaveWrapper,
    PeftConfig,
    PeftType,
    _freeze_adapter,
    _get_submodules,
    transpose,
)

NO_LORA = -100
LORA_BLOCK_MAPPING = []
BINARY_CODE = []
if is_bnb_available():
    import bitsandbytes as bnb


@dataclass
class MeloConfig(PeftConfig):
    """
    This is the configuration class to store the configuration of a [`LoraModel`].

    Args:
        r (`int`): Lora attention dimension.
        target_modules (`Union[List[str],str]`): The names of the modules to apply Lora to.
        lora_alpha (`int`): The alpha parameter for Lora scaling.
        lora_dropout (`float`): The dropout probability for Lora layers.
        fan_in_fan_out (`bool`): Set this to True if the layer to replace stores weight like (fan_in, fan_out).
        For example, gpt-2 uses `Conv1D` which stores weights like (fan_in, fan_out) and hence this should be set to `True`.:
        bias (`str`): Bias type for Lora. Can be 'none', 'all' or 'lora_only'
        modules_to_save (`List[str]`):List of modules apart from LoRA layers to be set as trainable
            and saved in the final checkpoint.
        layers_to_transform (`Union[List[int],int]`):
            The layer indexes to transform, if this argument is specified, it will apply the LoRA transformations on
            the layer indexes that are specified in this list. If a single integer is passed, it will apply the LoRA
            transformations on the layer at this index.
        layers_pattern (`str`):
            The layer pattern name, used only if `layers_to_transform` is different from `None` and if the layer
            pattern is not in the common layers pattern.
    """

    r: int = field(default=8, metadata={"help": "Lora attention dimension"})

    #melo_modified
    grace_layer: str = field(
        default = None,
        metadata = {
            "help":"Module name as a grace layer"
        }
    )

    grace_config: dict = field(
        default = None,
        metadata={
            "help": "Default settings of the grace layer"
        }
    )

    target_modules: Optional[Union[List[str], str]] = field(
        default=None,
        metadata={
            "help": "List of module names or regex expression of the module names to replace with Lora."
            "For example, ['q', 'v'] or '.*decoder.*(SelfAttention|EncDecAttention).*(q|v)$' "
        },
    )
    lora_alpha: int = field(default=8, metadata={"help": "Lora alpha"})
    lora_dropout: float = field(default=0.0, metadata={"help": "Lora dropout"})
    fan_in_fan_out: bool = field(
        default=False,
        metadata={"help": "Set this to True if the layer to replace stores weight like (fan_in, fan_out)"},
    )
    bias: str = field(default="none", metadata={"help": "Bias type for Lora. Can be 'none', 'all' or 'lora_only'"})
    modules_to_save: Optional[List[str]] = field(
        default=None,
        metadata={
            "help": "List of modules apart from LoRA layers to be set as trainable and saved in the final checkpoint. "
            "For example, in Sequence Classification or Token Classification tasks, "
            "the final layer `classifier/score` are randomly initialized and as such need to be trainable and saved."
        },
    )
    init_lora_weights: bool = field(
        default=True,
        metadata={"help": "Whether to initialize the weights of the Lora layers."},
    )
    layers_to_transform: Optional[Union[List, int]] = field(
        default=None,
        metadata={
            "help": "The layer indexes to transform, is this argument is specified, PEFT will transform only the layers indexes that are specified inside this list. If a single integer is passed, PEFT will transform only the layer at this index."
        },
    )
    layers_pattern: Optional[str] = field(
        default=None,
        metadata={
            "help": "The layer pattern name, used only if `layers_to_transform` is different to None and if the layer pattern is not in the common layers pattern."
        },
    )

    def __post_init__(self):
        self.peft_type = PeftType.MELO # PEFT识别的关键点！


class MeloModel(torch.nn.Module):
    def __init__(self, model, config, adapter_name):
        super().__init__()
        self.model = model
        self.forward = self.model.forward
        self.peft_config = config
        self.add_adapter(adapter_name, self.peft_config[adapter_name])
        self.add_grace(adapter_name,self.peft_config[adapter_name])

    def add_adapter(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_lora_config(config, model_config)
            self.peft_config[adapter_name] = config
        self._find_and_replace(adapter_name)
        if len(self.peft_config) > 1 and self.peft_config[adapter_name].bias != "none":
            raise ValueError(
                "LoraModel supports only 1 adapter with bias. When using multiple adapters, set bias to 'none' for all adapters."
            )
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)
        if self.peft_config[adapter_name].inference_mode:
            _freeze_adapter(self.model, adapter_name)
    def add_grace(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_melo_config(config, model_config)
            self.peft_config[adapter_name] = config
        self._find_and_replace_grace(adapter_name)
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)



    def _check_quantization_dependency(self):
        loaded_in_4bit = getattr(self.model, "is_loaded_in_4bit", False)
        loaded_in_8bit = getattr(self.model, "is_loaded_in_8bit", False)
        if (loaded_in_4bit or loaded_in_8bit) and not is_bnb_available():
            raise ImportError(
                "To use Lora with 8-bit or 4-bit quantization, please install the `bitsandbytes` package. "
                "You can install it with `pip install bitsandbytes`."
            )

    def _check_target_module_exists(self, lora_config, key):
        if isinstance(lora_config.target_modules, str):
            target_module_found = re.fullmatch(lora_config.target_modules, key)
        else:
            target_module_found = any(key.endswith(target_key) for target_key in lora_config.target_modules)
            is_using_layer_indexes = getattr(lora_config, "layers_to_transform", None) is not None
            layer_indexing_pattern = getattr(lora_config, "layers_pattern", None)

            if is_using_layer_indexes and target_module_found:
                layers_pattern = COMMON_LAYERS_PATTERN if layer_indexing_pattern is None else layer_indexing_pattern
                layers_pattern = [layers_pattern] if isinstance(layers_pattern, str) else layers_pattern

                for pattern in layers_pattern:
                    layer_index = re.match(f".*.{pattern}\.(\d+)\.*", key)
                    if layer_index is not None:
                        layer_index = int(layer_index.group(1))
                        if isinstance(lora_config.layers_to_transform, int):
                            target_module_found = layer_index == lora_config.layers_to_transform
                        else:
                            target_module_found = layer_index in lora_config.layers_to_transform

                        break
                    else:
                        target_module_found = False
        return target_module_found

    def _check_grace_layer_exists(self, grace_layer:str, key):
        target_module_found = re.fullmatch(grace_layer, key)

        return target_module_found



    def _create_new_module(self, lora_config, adapter_name, target):
        """ 造新的Linear层 """
        bias = hasattr(target, "bias") and target.bias is not None
        kwargs = {
            "r": lora_config.r,
            "lora_alpha": lora_config.lora_alpha,
            "lora_dropout": lora_config.lora_dropout,
            "fan_in_fan_out": lora_config.fan_in_fan_out,
            "init_lora_weights": lora_config.init_lora_weights,
            "num_rank_per_block":lora_config.grace_config['num_rank_per_block']
        }
        loaded_in_4bit = getattr(self.model, "is_loaded_in_4bit", False)
        loaded_in_8bit = getattr(self.model, "is_loaded_in_8bit", False)

        if loaded_in_8bit and isinstance(target, bnb.nn.Linear8bitLt):
            eightbit_kwargs = kwargs.copy()
            eightbit_kwargs.update(
                {
                    "has_fp16_weights": target.state.has_fp16_weights,
                    "memory_efficient_backward": target.state.memory_efficient_backward,
                    "threshold": target.state.threshold,
                    "index": target.index,
                }
            )
            new_module = Linear8bitLt(
                adapter_name, target.in_features, target.out_features, bias=bias, **eightbit_kwargs
            )
        elif loaded_in_4bit and is_bnb_4bit_available() and isinstance(target, bnb.nn.Linear4bit):
            fourbit_kwargs = kwargs.copy()
            fourbit_kwargs.update(
                {
                    "compute_dtype": target.compute_dtype,
                    "compress_statistics": target.weight.compress_statistics,
                    "quant_type": target.weight.quant_type,
                }
            )
            new_module = Linear4bit(adapter_name, target.in_features, target.out_features, bias=bias, **fourbit_kwargs)
        elif isinstance(target, torch.nn.Embedding):
            embedding_kwargs = kwargs.copy()
            embedding_kwargs.pop("fan_in_fan_out", None)
            in_features, out_features = target.num_embeddings, target.embedding_dim
            new_module = Embedding(adapter_name, in_features, out_features, **embedding_kwargs)
        elif isinstance(target, torch.nn.Conv2d):
            out_channels, in_channels = target.weight.size()[:2]
            kernel_size = target.weight.size()[2:]
            stride = target.stride
            padding = target.padding
            new_module = Conv2d(adapter_name, in_channels, out_channels, kernel_size, stride, padding, **kwargs)
        else:
            if isinstance(target, torch.nn.Linear):
                in_features, out_features = target.in_features, target.out_features
                if kwargs["fan_in_fan_out"]:
                    warnings.warn(
                        "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                        "Setting fan_in_fan_out to False."
                    )
                    kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = False
            elif isinstance(target, Conv1D):
                in_features, out_features = (
                    target.weight.ds_shape if hasattr(target.weight, "ds_shape") else target.weight.shape
                )
                if not kwargs["fan_in_fan_out"]:
                    warnings.warn(
                        "fan_in_fan_out is set to False but the target module is `Conv1D`. "
                        "Setting fan_in_fan_out to True."
                    )
                    kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = True
            else:
                raise ValueError(
                    f"Target module {target} is not supported. "
                    f"Currently, only `torch.nn.Linear` and `Conv1D` are supported."
                )
            new_module = Linear(adapter_name, in_features, out_features, bias=bias, **kwargs)

        return new_module

    def _create_new_grace_module(self, config, adapter_name, target):
        bias = hasattr(target, "bias") and target.bias is not None
        kwargs = {
            "fan_in_fan_out": config.fan_in_fan_out,
        }

        if isinstance(target, torch.nn.Linear):
            in_features, out_features = target.in_features, target.out_features
            if kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                    "Setting fan_in_fan_out to False."
                )
                kwargs["fan_in_fan_out"] = config.fan_in_fan_out = False
        elif isinstance(target, Conv1D):
            in_features, out_features = (
                target.weight.ds_shape if hasattr(target.weight, "ds_shape") else target.weight.shape
            )
            if not kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to False but the target module is `Conv1D`. "
                    "Setting fan_in_fan_out to True."
                )
                kwargs["fan_in_fan_out"] = config.fan_in_fan_out = True
        else:
            raise ValueError(
                f"Target grace module {target} is not supported. "
                f"Currently, only `torch.nn.Linear` and 'torch.nn.Conv1D' are supported."
            )
        new_module = GraceLinear(adapter_name, in_features, out_features, config.grace_config, bias=bias, **kwargs)

        return new_module

    def _find_and_replace_grace(self, adapter_name):
        config = self.peft_config[adapter_name]
        is_target_module_in_base_model = False
        grace_layer = config.grace_layer
        key_list = [key for key,_ in self.model.named_modules()]

        for key in key_list:
            if not self._check_grace_layer_exists(grace_layer,key):
                continue
            print(f"Target Grace Layer is found: {key}")
            is_target_module_in_base_model = True
            parent, target, target_name = _get_submodules(self.model, key)
            if isinstance(target, LoraLayer):
                raise ValueError("Cannot set LoraLayer as GraceLayer")
            new_module = self._create_new_grace_module(config, adapter_name, target)
            self._replace_module(parent, target_name, new_module, target)
        if not is_target_module_in_base_model:
            raise ValueError(
                f"Target grace modules {config.model.grace_layer} not found in the base model. "
                f"Please check the target modules and try again."
            )



    def _find_and_replace(self, adapter_name):
        lora_config = self.peft_config[adapter_name]

        self._check_quantization_dependency()
        is_target_modules_in_base_model = False # LoRA的参数不在base model里
        key_list = [key for key, _ in self.model.named_modules()]

        for key in key_list:
            if not self._check_target_module_exists(lora_config, key): # 迭代直到找到要处理的模型参数
                continue

            is_target_modules_in_base_model = True
            parent, target, target_name = _get_submodules(self.model, key)

            if isinstance(target, LoraLayer) and isinstance(target, torch.nn.Conv2d):
                target.update_layer_conv2d(
                    adapter_name,
                    lora_config.r,
                    lora_config.lora_alpha,
                    lora_config.lora_dropout,
                    lora_config.init_lora_weights,
                )
            elif isinstance(target, LoraLayer):
                target.update_layer(
                    adapter_name,
                    lora_config.r,
                    lora_config.lora_alpha,
                    lora_config.lora_dropout,
                    lora_config.init_lora_weights,
                    lora_config.grace_config['num_rank_per_block']
                )
            elif isinstance(target, GraceLayer):
                raise ValueError("Cannot set GraceLayer as LoraLayer")
            else:
                new_module = self._create_new_module(lora_config, adapter_name, target)
                self._replace_module(parent, target_name, new_module, target)

        if not is_target_modules_in_base_model:
            raise ValueError(
                f"Target modules {lora_config.target_modules} not found in the base model. "
                f"Please check the target modules and try again."
            )

    def _replace_module(self, parent_module, child_name, new_module, old_module):
        setattr(parent_module, child_name, new_module)
        new_module.weight = old_module.weight
        if hasattr(old_module, "bias"):
            if old_module.bias is not None:
                new_module.bias = old_module.bias

        if getattr(old_module, "state", None) is not None:
            new_module.state = old_module.state
            new_module.to(old_module.weight.device)

        # dispatch to correct device
        for name, module in new_module.named_modules():
            if "lora_" in name:
                module.to(old_module.weight.device)
            if "ranknum" in name:
                module.to(old_module.weight.device)


    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.model, name)

    def get_peft_config_as_dict(self, inference: bool = False):
        config_dict = {}
        for key, value in self.peft_config.items():
            config = {k: v.value if isinstance(v, Enum) else v for k, v in asdict(value).items()}
            if inference:
                config["inference_mode"] = True
        config_dict[key] = config
        return config

    def _set_adapter_layers(self, enabled=True):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.disable_adapters = False if enabled else True

    def enable_adapter_layers(self):
        self._set_adapter_layers(enabled=True)

    def disable_adapter_layers(self):
        self._set_adapter_layers(enabled=False)

    def disable_grace_layer(self):
        for module in self.model.modules():
            if isinstance(module, GraceLayer):
                module.disable_grace = True

    def enable_grace_layer(self):
        for module in self.model.modules():
            if isinstance(module, GraceLayer):
                module.disable_grace = False


    def set_adapter(self, adapter_name):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                if module.merged:
                    warnings.warn("Adapter cannot be set when the model is merged. Unmerging the model first.")
                    module.unmerge()
                module.active_adapter = adapter_name

    def merge_adapter(self):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.merge()

    def unmerge_adapter(self):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.unmerge()

    @staticmethod
    def _prepare_lora_config(peft_config, model_config):
        if peft_config.target_modules is None:
            if model_config["model_type"] not in TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING:
                raise ValueError("Please specify `target_modules` in `peft_config`")
            peft_config.target_modules = TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING[model_config["model_type"]]
        return peft_config
    @staticmethod
    def _prepare_melo_config(peft_config, model_config):
        if peft_config.grace_layer is None:
                raise ValueError("Please specify `grace_layer` in `peft_config`")
        return peft_config


    @staticmethod
    def _prepare_grace_config(peft_config, model_config):
        if peft_config.grace_layer is None or peft_config.grace_config is None:
            if model_config["model_type"] not in TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING:
                raise ValueError("Please specify `grace_layer` and `grace_config` in `peft_config`")
        return peft_config

    def merge_and_unload(self):
        r"""
        This method merges the LoRa layers into the base model. This is needed if someone wants to use the base model
        as a standalone model.
        """
        if getattr(self.config, "model_type", None) == "gpt2":
            raise ValueError("GPT2 models are not supported for merging LORA layers")

        if getattr(self.model, "is_loaded_in_8bit", False) or getattr(self.model, "is_loaded_in_4bit", False):
            raise ValueError("Cannot merge LORA layers when the model is loaded in 8-bit mode")

        key_list = [key for key, _ in self.model.named_modules() if "lora" not in key]
        for key in key_list:
            try:
                parent, target, target_name = _get_submodules(self.model, key)
            except AttributeError:
                continue
            if isinstance(target, LoraLayer):
                if isinstance(target, nn.Embedding):
                    new_module = torch.nn.Embedding(target.in_features, target.out_features)
                else:
                    bias = target.bias is not None
                    new_module = torch.nn.Linear(target.in_features, target.out_features, bias=bias)
                target.merge()
                self._replace_module(parent, target_name, new_module, target)

            # save any additional trainable modules part of `modules_to_save`
            if isinstance(target, ModulesToSaveWrapper):
                setattr(parent, target_name, target.modules_to_save[target.active_adapter])

        return self.model

    def add_weighted_adapter(self, adapters, weights, adapter_name):
        if len({self.peft_config[adapter].r for adapter in adapters}) != 1:
            raise ValueError("All adapters must have the same r value")
        self.peft_config[adapter_name] = self.peft_config[adapters[0]]
        self.peft_config[adapter_name].lora_alpha = self.peft_config[adapters[0]].r
        self._find_and_replace(adapter_name)
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)
        _freeze_adapter(self.model, adapter_name)
        key_list = [key for key, _ in self.model.named_modules() if "lora" not in key]
        for key in key_list:
            _, target, _ = _get_submodules(self.model, key)
            if isinstance(target, LoraLayer):
                if adapter_name in target.lora_A:
                    target.lora_A[adapter_name].weight.data = target.lora_A[adapter_name].weight.data * 0.0
                    target.lora_B[adapter_name].weight.data = target.lora_B[adapter_name].weight.data * 0.0
                    for adapter, weight in zip(adapters, weights):
                        if adapter not in target.lora_A:
                            continue
                        target.lora_A[adapter_name].weight.data += (
                            target.lora_A[adapter].weight.data * weight * target.scaling[adapter]
                        )
                        target.lora_B[adapter_name].weight.data += target.lora_B[adapter].weight.data * weight

                elif adapter_name in target.lora_embedding_A:
                    target.lora_embedding_A[adapter_name].data = target.lora_embedding_A[adapter_name].data * 0.0
                    target.lora_embedding_B[adapter_name].data = target.lora_embedding_B[adapter_name].data * 0.0
                    for adapter, weight in zip(adapters, weights):
                        if adapter not in target.lora_embedding_A:
                            continue
                        target.lora_embedding_A[adapter_name].data += (
                            target.lora_embedding_A[adapter].data * weight * target.scaling[adapter]
                        )
                        target.lora_embedding_B[adapter_name].data += target.lora_embedding_B[adapter].data * weight


@dataclass
class ELDERConfig(PeftConfig):
    """
    This is the configuration class to store the configuration of ELDER model 
    """
    num_iters: int = field(default=None, metadata={"help": "number of training iterations"})

    r: int = field(default=8, metadata={"help": "Lora attention dimension"})

    num_experts: int = field(default=8, metadata={"help": "Num of experts in LoRA-MoE"})

    is_redundant_experts: bool = field(default=False)

    #melo_modified
    grace_layer: str = field(
        default = None,
        metadata = {
            "help":"Module name as a grace layer"
        }
    )

    grace_config: dict = field(
        default = None,
        metadata={
            "help": "Default settings of the grace layer"
        }
    )

    target_modules: Optional[Union[List[str], str]] = field(
        default=None,
        metadata={
            "help": "List of module names or regex expression of the module names to replace with Lora."
            "For example, ['q', 'v'] or '.*decoder.*(SelfAttention|EncDecAttention).*(q|v)$' "
        },
    )
    lora_alpha: int = field(default=8, metadata={"help": "Lora alpha"})
    lora_dropout: float = field(default=0.0, metadata={"help": "Lora dropout"})
    fan_in_fan_out: bool = field(
        default=False,
        metadata={"help": "Set this to True if the layer to replace stores weight like (fan_in, fan_out)"},
    )
    bias: str = field(default="none", metadata={"help": "Bias type for Lora. Can be 'none', 'all' or 'lora_only'"})
    modules_to_save: Optional[List[str]] = field(
        default=None,
        metadata={
            "help": "List of modules apart from LoRA layers to be set as trainable and saved in the final checkpoint. "
            "For example, in Sequence Classification or Token Classification tasks, "
            "the final layer `classifier/score` are randomly initialized and as such need to be trainable and saved."
        },
    )
    init_lora_weights: bool = field(
        default=True,
        metadata={"help": "Whether to initialize the weights of the Lora layers."},
    )
    layers_to_transform: Optional[Union[List, int]] = field(
        default=None,
        metadata={
            "help": "The layer indexes to transform, is this argument is specified, PEFT will transform only the layers indexes that are specified inside this list. If a single integer is passed, PEFT will transform only the layer at this index."
        },
    )
    layers_pattern: Optional[str] = field(
        default=None,
        metadata={
            "help": "The layer pattern name, used only if `layers_to_transform` is different to None and if the layer pattern is not in the common layers pattern."
        },
    )

    def __post_init__(self):
        self.peft_type = PeftType.ELDER # PEFT module: ELDER


class ELDERModel(torch.nn.Module):
    def __init__(self, model, config, adapter_name):
        super().__init__()
        self.model = model
        self.forward = self.model.forward
        self.peft_config = config
        self.device = model.device

        self.add_my_adapter(adapter_name, self.peft_config[adapter_name])
        self.add_grace(adapter_name, self.peft_config[adapter_name]) # 用GRACE记录并固定每个edit的表示向量

    def add_my_adapter(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_lora_config(config, model_config) # 检查config是否指定了修改模型哪个位置
            self.peft_config[adapter_name] = config
        self.gate_linear_layers = self._find_and_replace_my_adapter(adapter_name)
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)
        if self.peft_config[adapter_name].inference_mode:
            _freeze_adapter(self.model, adapter_name)


    def add_adapter(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_lora_config(config, model_config)
            self.peft_config[adapter_name] = config
        self._find_and_replace(adapter_name)
        if len(self.peft_config) > 1 and self.peft_config[adapter_name].bias != "none":
            raise ValueError(
                "LoraModel supports only 1 adapter with bias. When using multiple adapters, set bias to 'none' for all adapters."
            )
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)
        if self.peft_config[adapter_name].inference_mode:
            _freeze_adapter(self.model, adapter_name)
    def add_grace(self, adapter_name, config=None):
        if config is not None:
            model_config = self.model.config.to_dict() if hasattr(self.model.config, "to_dict") else self.model.config
            config = self._prepare_melo_config(config, model_config)
            self.peft_config[adapter_name] = config
        self._find_and_replace_grace(adapter_name)
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)



    def _check_quantization_dependency(self):
        loaded_in_4bit = getattr(self.model, "is_loaded_in_4bit", False)
        loaded_in_8bit = getattr(self.model, "is_loaded_in_8bit", False)
        if (loaded_in_4bit or loaded_in_8bit) and not is_bnb_available():
            raise ImportError(
                "To use Lora with 8-bit or 4-bit quantization, please install the `bitsandbytes` package. "
                "You can install it with `pip install bitsandbytes`."
            )

    def _check_target_module_exists(self, lora_config, key):
        if isinstance(lora_config.target_modules, str):
            target_module_found = re.fullmatch(lora_config.target_modules, key)
        else:
            target_module_found = any(key.endswith(target_key) for target_key in lora_config.target_modules)
            is_using_layer_indexes = getattr(lora_config, "layers_to_transform", None) is not None
            layer_indexing_pattern = getattr(lora_config, "layers_pattern", None)

            if is_using_layer_indexes and target_module_found:
                layers_pattern = COMMON_LAYERS_PATTERN if layer_indexing_pattern is None else layer_indexing_pattern
                layers_pattern = [layers_pattern] if isinstance(layers_pattern, str) else layers_pattern

                for pattern in layers_pattern:
                    layer_index = re.match(f".*.{pattern}\.(\d+)\.*", key)
                    if layer_index is not None:
                        layer_index = int(layer_index.group(1))
                        if isinstance(lora_config.layers_to_transform, int):
                            target_module_found = layer_index == lora_config.layers_to_transform
                        else:
                            target_module_found = layer_index in lora_config.layers_to_transform

                        break
                    else:
                        target_module_found = False
        return target_module_found

    def _check_grace_layer_exists(self, grace_layer:str, key):
        target_module_found = re.fullmatch(grace_layer, key)

        return target_module_found

    def _find_and_replace_my_adapter(self, adapter_name):
        lora_config = self.peft_config[adapter_name]
        self._check_quantization_dependency() # 没啥用
        is_target_modules_in_base_model = False
        key_list = [key for key, _ in self.model.named_modules()]
        gate_linear_layers = [] 

        for key in key_list:
            if not self._check_target_module_exists(lora_config, key):
                continue
            is_target_modules_in_base_model = True
            parent, target, target_name = _get_submodules(self.model, key)
            new_module = self._create_new_module(lora_config, adapter_name, target)
            self._replace_module(parent, target_name, new_module, target)
            gate_linear_layers.append(new_module.gate.gate)

        if not is_target_modules_in_base_model:
            raise ValueError(
                f"Target modules {lora_config.target_modules} not found in the base model. "
                f"Please check the target modules and try again."
            )
        
        return gate_linear_layers

    def _create_new_grace_module(self, config, adapter_name, target):
        bias = hasattr(target, "bias") and target.bias is not None
        kwargs = {
            "fan_in_fan_out": config.fan_in_fan_out,
            "top_k": 2,
            "num_experts": config.num_experts,
        }

        if isinstance(target, torch.nn.Linear):
            in_features, out_features = target.in_features, target.out_features
            if kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                    "Setting fan_in_fan_out to False."
                )
                kwargs["fan_in_fan_out"] = config.fan_in_fan_out = False
        elif isinstance(target, Conv1D):
            in_features, out_features = (
                target.weight.ds_shape if hasattr(target.weight, "ds_shape") else target.weight.shape
            )
            if not kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to False but the target module is `Conv1D`. "
                    "Setting fan_in_fan_out to True."
                )
                kwargs["fan_in_fan_out"] = config.fan_in_fan_out = True
        else:
            raise ValueError(
                f"Target grace module {target} is not supported. "
                f"Currently, only `torch.nn.Linear` and 'torch.nn.Conv1D' are supported."
            )
        new_module = ElderGraceLinear(adapter_name, in_features, out_features, config.grace_config, bias=bias, gate_linears=self.gate_linear_layers, **kwargs)

        return new_module

    def _find_and_replace_grace(self, adapter_name):
        config = self.peft_config[adapter_name]
        is_target_module_in_base_model = False
        grace_layer = config.grace_layer
        key_list = [key for key,_ in self.model.named_modules()]

        for key in key_list:
            if not self._check_grace_layer_exists(grace_layer,key):
                continue
            print(f"Target Grace Layer is found: {key}")
            is_target_module_in_base_model = True
            parent, target, target_name = _get_submodules(self.model, key)
            if isinstance(target, LoraLayer):
                raise ValueError("Cannot set LoraLayer as GraceLayer")
            new_module = self._create_new_grace_module(config, adapter_name, target)
            self._replace_module(parent, target_name, new_module, target)
        if not is_target_module_in_base_model:
            raise ValueError(
                f"Target grace modules {config.model.grace_layer} not found in the base model. "
                f"Please check the target modules and try again."
            )



    def _create_new_module(self, lora_config, adapter_name, target):
        """ create new Linear layer """
        bias = hasattr(target, "bias") and target.bias is not None
        kwargs = {
            "r": lora_config.r,
            "lora_alpha": lora_config.lora_alpha,
            "num_experts": lora_config.num_experts,
            "lora_dropout": lora_config.lora_dropout,
            "fan_in_fan_out": lora_config.fan_in_fan_out,
            "init_lora_weights": lora_config.init_lora_weights,
            "is_redundant_experts": lora_config.is_redundant_experts,
        }

        if isinstance(target, torch.nn.Linear):
            in_features, out_features = target.in_features, target.out_features
            if kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                    "Setting fan_in_fan_out to False."
                )
                kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = False
        elif isinstance(target, Conv1D):
            in_features, out_features = (
                target.weight.ds_shape if hasattr(target.weight, "ds_shape") else target.weight.shape
            )
            if not kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to False but the target module is `Conv1D`. "
                    "Setting fan_in_fan_out to True."
                )
                kwargs["fan_in_fan_out"] = lora_config.fan_in_fan_out = True
        else:
            raise ValueError(
                f"Target module {target} is not supported. "
                f"Currently, only `torch.nn.Linear` and `Conv1D` are supported."
            )
        new_module = ElderLinear(adapter_name, in_features, out_features, bias=bias, **kwargs)

        return new_module


    def _replace_module(self, parent_module, child_name, new_module, old_module):
        setattr(parent_module, child_name, new_module)
        new_module.weight = old_module.weight
        if hasattr(old_module, "bias"):
            if old_module.bias is not None:
                new_module.bias = old_module.bias

        if getattr(old_module, "state", None) is not None:
            new_module.state = old_module.state
            new_module.to(old_module.weight.device)

        # dispatch to correct device
        for name, module in new_module.named_modules():
            if "lora_" in name:
                module.to(old_module.weight.device)
            if "ranknum" in name:
                module.to(old_module.weight.device)


    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.model, name)

    def get_peft_config_as_dict(self, inference: bool = False):
        config_dict = {}
        for key, value in self.peft_config.items():
            config = {k: v.value if isinstance(v, Enum) else v for k, v in asdict(value).items()}
            if inference:
                config["inference_mode"] = True
        config_dict[key] = config
        return config

    def _set_adapter_layers(self, enabled=True):
        for module in self.model.modules():
            if isinstance(module, ElderLinear):
                module.disable_adapters = False if enabled else True

    def enable_adapter_layers(self):
        self._set_adapter_layers(enabled=True)

    def disable_adapter_layers(self):
        self._set_adapter_layers(enabled=False)

    def disable_grace_layer(self):
        for module in self.model.modules():
            if isinstance(module, GraceLayer):
                module.disable_grace = True

    def enable_grace_layer(self):
        for module in self.model.modules():
            if isinstance(module, GraceLayer):
                module.disable_grace = False


    def set_adapter(self, adapter_name):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                if module.merged:
                    warnings.warn("Adapter cannot be set when the model is merged. Unmerging the model first.")
                    module.unmerge()
                module.active_adapter = adapter_name

    def merge_adapter(self):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.merge()

    def unmerge_adapter(self):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                module.unmerge()

    @staticmethod
    def _prepare_lora_config(peft_config, model_config):
        if peft_config.target_modules is None:
            if model_config["model_type"] not in TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING:
                raise ValueError("Please specify `target_modules` in `peft_config`")
            peft_config.target_modules = TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING[model_config["model_type"]]
        return peft_config
    @staticmethod
    def _prepare_melo_config(peft_config, model_config):
        if peft_config.grace_layer is None:
                raise ValueError("Please specify `grace_layer` in `peft_config`")
        return peft_config

    def merge_and_unload(self):
        r"""
        This method merges the LoRa layers into the base model. This is needed if someone wants to use the base model
        as a standalone model.
        """
        if getattr(self.config, "model_type", None) == "gpt2":
            raise ValueError("GPT2 models are not supported for merging LORA layers")

        if getattr(self.model, "is_loaded_in_8bit", False) or getattr(self.model, "is_loaded_in_4bit", False):
            raise ValueError("Cannot merge LORA layers when the model is loaded in 8-bit mode")

        key_list = [key for key, _ in self.model.named_modules() if "lora" not in key]
        for key in key_list:
            try:
                parent, target, target_name = _get_submodules(self.model, key)
            except AttributeError:
                continue
            if isinstance(target, LoraLayer):
                if isinstance(target, nn.Embedding):
                    new_module = torch.nn.Embedding(target.in_features, target.out_features)
                else:
                    bias = target.bias is not None
                    new_module = torch.nn.Linear(target.in_features, target.out_features, bias=bias)
                target.merge()
                self._replace_module(parent, target_name, new_module, target)

            # save any additional trainable modules part of `modules_to_save`
            if isinstance(target, ModulesToSaveWrapper):
                setattr(parent, target_name, target.modules_to_save[target.active_adapter])

        return self.model

    def add_weighted_adapter(self, adapters, weights, adapter_name):
        if len({self.peft_config[adapter].r for adapter in adapters}) != 1:
            raise ValueError("All adapters must have the same r value")
        self.peft_config[adapter_name] = self.peft_config[adapters[0]]
        self.peft_config[adapter_name].lora_alpha = self.peft_config[adapters[0]].r
        self._find_and_replace(adapter_name)
        mark_only_lora_as_trainable(self.model, self.peft_config[adapter_name].bias)
        _freeze_adapter(self.model, adapter_name)
        key_list = [key for key, _ in self.model.named_modules() if "lora" not in key]
        for key in key_list:
            _, target, _ = _get_submodules(self.model, key)
            if isinstance(target, LoraLayer):
                if adapter_name in target.lora_A:
                    target.lora_A[adapter_name].weight.data = target.lora_A[adapter_name].weight.data * 0.0
                    target.lora_B[adapter_name].weight.data = target.lora_B[adapter_name].weight.data * 0.0
                    for adapter, weight in zip(adapters, weights):
                        if adapter not in target.lora_A:
                            continue
                        target.lora_A[adapter_name].weight.data += (
                            target.lora_A[adapter].weight.data * weight * target.scaling[adapter]
                        )
                        target.lora_B[adapter_name].weight.data += target.lora_B[adapter].weight.data * weight

                elif adapter_name in target.lora_embedding_A:
                    target.lora_embedding_A[adapter_name].data = target.lora_embedding_A[adapter_name].data * 0.0
                    target.lora_embedding_B[adapter_name].data = target.lora_embedding_B[adapter_name].data * 0.0
                    for adapter, weight in zip(adapters, weights):
                        if adapter not in target.lora_embedding_A:
                            continue
                        target.lora_embedding_A[adapter_name].data += (
                            target.lora_embedding_A[adapter].data * weight * target.scaling[adapter]
                        )
                        target.lora_embedding_B[adapter_name].data += target.lora_embedding_B[adapter].data * weight



# Below code is based on https://github.com/microsoft/LoRA/blob/main/loralib/layers.py
# and modified to work with PyTorch FSDP


#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------


# had to adapt it for `lora_only` to work
def mark_only_lora_as_trainable(model: nn.Module, bias: str = "none") -> None:
    for n, p in model.named_parameters():
        # if "lora_" not in n:
        if "lora_" not in n and "gate" not in n:
            p.requires_grad = False
        if "gate_proj" in n: # 处理Llama2里的mlp.gate_proj
            p.requires_grad = False
    # for name, m in model.named_modules():
    #     if isinstance(m, Gate):
    #         for n, p in m.named_parameters():
    #             p.requires_grad = True

    if bias == "none":
        return
    elif bias == "all":
        for n, p in model.named_parameters():
            if "bias" in n:
                p.requires_grad = True
    elif bias == "lora_only":
        for m in model.modules():
            if isinstance(m, LoraLayer) and hasattr(m, "bias") and m.bias is not None:
                m.bias.requires_grad = True
    else:
        raise NotImplementedError


class mem_point:
    def __init__(self, key, value):
        self.key = key
        self.value = value
    def get_key(self):
        return self.key

    def get_value(self):
        return self.value

    def get_lora_id(self):
        return self.value


class VecDB:
    def __init__(self, grace_config):
        self.config = grace_config
        self.table = []
        self.forget_num = 0
        self.conflict_num = 0
        self.forget_keys = []

    def __len__(self):
        return len(self.table)

    def __getitem__(self, item):
        return self.table[item]

    def add_cluster(self, new_key, new_value, new_edit_label):
        new_row = {'cluster_center':None, 'radius': None, 'key_label': None, 'points':[]}

        new_row['cluster_center'] = new_key.detach()
        new_row['radius'] = torch.tensor(self.config['init_radius'], device = new_key.device).view(1)
        new_row['key_label'] = new_edit_label
        new_row['points'].append(mem_point(new_key.detach(),new_value))

        self.table.append(new_row)

    def update_cluster(self, index, new_key, new_value):
        self.table[index]['points'].append(mem_point(new_key,new_value))
        key_list = [x.get_key() for x in self.table[index]['points']]
        new_cluster_center = sum(key_list)/len(key_list)
        self.table[index]['cluster_center'] = new_cluster_center

        dists = self.euc(key_list, new_cluster_center).view(-1,1)
        largest_distance, _ = dists.max(0)
        self.table[index]['radius'] = max(largest_distance, torch.tensor(self.config['init_radius'], device = new_key.device).view(1))


    def label_match(self, edit_label, key_label):
        edit_label = edit_label.masked_fill(edit_label == -100, 0)
        key_label = key_label.masked_fill(key_label == -100, 0)
        return torch.sum(edit_label) == torch.sum(key_label)

    def split_cluster_radii_in_half(self, nearest_cluster, smallest_distance):
        self.table[nearest_cluster]['radius'] = (smallest_distance / 2) - 1e-5
        self.table[-1]['radius'] = (smallest_distance / 2) + 1e-5

        cluster_radius = self.table[nearest_cluster]['radius']
        key_list = [x.get_key() for x in self.table[nearest_cluster]['points']]
        key_list = torch.stack(key_list,dim=0)
        cluster_center = self.table[nearest_cluster]['cluster_center']
        dists = self.euc(key_list,cluster_center).view(-1,1)
        filtered_key_list = []
        for index, dist in enumerate(dists):
            if dist <= cluster_radius:
                filtered_key_list.append(self.table[nearest_cluster]['points'][index])
            else:
                self.forget_keys.append(key_list[index])
        if len(filtered_key_list) == 0:
            filtered_key_list.append(mem_point(cluster_center,NO_LORA))
        self.table[nearest_cluster]['points'] = filtered_key_list

        self.forget_num += len(key_list) - len(filtered_key_list)
        self.conflict_num += 1




    def euc(self, batch_query, key):
        if isinstance(batch_query, list):
            batch_query = torch.stack(batch_query,dim=0)
        # Euclidean distance
        if len(key.shape) < 2:
            key = key.view(1, -1)
        return torch.cdist(batch_query,key, p=2, compute_mode='donot_use_mm_for_euclid_dist')

    def search_database(self, batch_query):
        dists = []
        for x in self.table:
            dists.append(self.euc(batch_query,x['cluster_center']).view(-1,1))
        dists = torch.stack(dists).view(-1, len(batch_query))
        smallest_distance_list, nearest_cluster_list = dists.min(0)
        return smallest_distance_list,nearest_cluster_list

    def search_cluster(self, batch_query, smallest_distance_list,  nearest_cluster_list):
        lora_mapping_block = []
        for query, smallest_distance, nearest_cluster in zip(batch_query, smallest_distance_list,nearest_cluster_list):
            try:
                if smallest_distance > self.table[nearest_cluster]['radius']:
                    lora_mapping_block.append(NO_LORA)
                    continue
                # Valid Cluster
                key_list = [x.get_key() for x in self.table[nearest_cluster]['points']]
                key_list = torch.stack(key_list, dim=0)
                dists = self.euc(key_list,query).view(-1,1)
                _, nearest_key = dists.min(0)
                lora_mapping_block.append(self.table[nearest_cluster]['points'][nearest_key].get_value())
            except Exception as e:
                print(e)
                print(f'[smallest_distace]: {smallest_distance}')
                print(f"[nearest_cluster]: {self.table[nearest_cluster]['radius']}")

        return lora_mapping_block








class GraceLayer:
    """ Layer of Grace in "Aging with GRACE: Lifelong Model Editing with Discrete Key-Value Adaptors" """
    def __init__(self, grace_config: dict, in_features: int, out_features: int, **kwargs):
        self.grace_config = grace_config
        self.batch_iter = None
        for k, v in grace_config.items():
            setattr(self, k, v)
        self.VecDB = VecDB(grace_config)
        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs
        self.lora_block_mapping = []
        self.non_overlap_edit = 0
        self.disable_grace = False
        self.block_id = 0



    def search(self, batch_query):
        smallest_distance_list, nearest_cluster_list = self.VecDB.search_database(batch_query)
        lora_block_mapping = self.VecDB.search_cluster(batch_query, smallest_distance_list, nearest_cluster_list)
        return smallest_distance_list, nearest_cluster_list, lora_block_mapping

    def current_block(self):
        return self.block_id
            
        

    def init_key_value(self, batch_query):
        lora_block_mapping = []
        for index, query in enumerate(batch_query):
            new_key = query.detach()
            new_eidt_label = self.edit_label[index]
            new_value = self.current_block()
            self.VecDB.add_cluster(new_key= new_key, new_value=new_value, new_edit_label= new_eidt_label)
            lora_block_mapping.append(new_value)
            # self.block_id += 1
        self.block_id += 1
        return lora_block_mapping

    def add_cluster(self, query, label_index):
        new_key = query.detach()
        new_value = self.current_block()
        new_edit_label = self.edit_label[label_index]
        self.VecDB.add_cluster(new_key = new_key, new_value = new_value, new_edit_label=new_edit_label)
        # self.block_id += 1

    
    def update_cluster(self, index, query, value):
         self.VecDB.update_cluster(index,query.detach(), value)

class GraceLinear(nn.Linear, GraceLayer):
    def __init__(
            self,
            adapter_name: str,
            in_features: int,
            out_features: int,
            grace_config: dict,
            fan_in_fan_out: bool = False, # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
            **kwargs
    ):
        nn.Linear.__init__(self, in_features, out_features,**kwargs)
        GraceLayer.__init__(self,grace_config=grace_config, in_features=in_features, out_features=out_features)
        self.fan_in_fan_out = fan_in_fan_out
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

    def forward(self, x: torch.Tensor):
        global LORA_BLOCK_MAPPING
        layer_out = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        if self.disable_grace:
            return layer_out

        '''Search Vector Database
        '''
        key_id = min(self.key_id, layer_out.shape[1] - 1)
        batch_query = layer_out[:, key_id]
        smallest_distance_list, nearest_cluster_list, lora_block_mapping = None, None, [NO_LORA] * layer_out.shape[0]
        if len(self.VecDB) != 0:
            smallest_distance_list, nearest_cluster_list, lora_block_mapping = self.search(batch_query)

        if not self.training:
            self.lora_block_mapping = LORA_BLOCK_MAPPING = lora_block_mapping
            return layer_out



        '''Only update the vector database once per batch
        '''
        if len(self.VecDB) == 0:
            self.lora_block_mapping = self.init_key_value(batch_query)
        elif self.batch_iter == 0:
            self.lora_block_mapping = [self.block_id] * layer_out.shape[0]
            for index, query in enumerate(batch_query):
                row = self.VecDB[nearest_cluster_list[index]]
                if smallest_distance_list[index] > row['radius'] + self.init_radius:
                    self.add_cluster(query,label_index=index)
                elif self.VecDB.label_match(self.edit_label[index], row['key_label']):
                    print(f'The {index}th query is close to a previous edit, the labels are the same')
                    self.update_cluster(nearest_cluster_list[index],query,self.block_id)
                else:
                    print(f'The {index}th query is close to a previous edit, but the labels are different')
                    self.add_cluster(query,label_index = index)
                    self.VecDB.split_cluster_radii_in_half(nearest_cluster_list[index], smallest_distance_list[index])
            self.block_id += 1
        else:
            pass

        LORA_BLOCK_MAPPING = self.lora_block_mapping
        return layer_out

class dynamic(nn.Module):
    def __init__(
            self,
            maximum_rank: int = 1,
            num_rank_per_block: int = 1
    ):
        assert maximum_rank % num_rank_per_block == 0, \
            "Maximum_rank % num_rank_per_block == 0 should be True"
        super(dynamic, self).__init__()
        self.maximum_rank = maximum_rank
        self.num_rank_per_block = num_rank_per_block
        self.maximum_block = maximum_rank // num_rank_per_block
        self.current_block = 0
    def get_block_dimension(self):
        return self.maximum_block

    def get_block(self):
        return self.current_block

    def set_block(self, block):
        self.current_block = max(0, min(block, self.get_block()))

    def block_rank_mapping(self, block_id):
        start = block_id * self.num_rank_per_block
        end = start + self.num_rank_per_block
        return start, end

    def update_dynamic(self, maximum_rank, num_rank_per_block):
        assert maximum_rank % num_rank_per_block == 0, \
            "Maximum_rank % num_rank_per_block == 0 should be True"
        self.maximum_rank = maximum_rank
        self.num_rank_per_block = num_rank_per_block
        self.maximum_block = maximum_rank // num_rank_per_block
        self.current_block = 0

    def forward(self, inputs):
        block_list = []
        assert len(LORA_BLOCK_MAPPING) != 0, "No element in LORA_BLOCK_MAPPING"
        for block_id in LORA_BLOCK_MAPPING:
            if block_id == NO_LORA:
                zero_tensor = torch.zeros((self.num_rank_per_block,inputs.shape[1]),device=inputs.device)
                block_list.append(zero_tensor)
            else:
                start, end = self.block_rank_mapping(block_id)
                block_list.append(inputs[start:end])
        result = torch.stack(block_list,0)

        return result * math.sqrt(self.maximum_rank / self.num_rank_per_block)

class Gate(nn.Module):
    """ https://github.com/laekov/fastmoe/blob/master/fmoe/gates/naive_gate.py#L11 """
    def __init__(self, d_model, num_expert, top_k=2, gate_bias=True) -> None:
        super().__init__()
        self.num_expert = num_expert
        self.loss = None

        self.gate = nn.Linear(d_model, self.num_expert, bias=gate_bias)
        self.top_k = min(top_k, num_expert)
        self.preset_target = False # use preset LoRA allocation
        self.target_set_mode = None
        self.editing = False

    def forward(self, inp, return_all_scores=False):
        """
        Router for Mixture-of-LoRA
        """
        gate = self.gate(inp)
        gate = F.softmax(gate, dim=-1)
        if self.preset_target and self.target_set_mode == 'hard':
            gate_top_k_idx = self.preset_gate_top_k_idx
            gate_top_k_val = torch.gather(input=gate, index=gate_top_k_idx, dim=-1)
        else:
            gate_top_k_val, gate_top_k_idx = torch.topk(
                gate, k=self.top_k, dim=-1, largest=True, sorted=False
            )  # [.. x top_k]
            gate_top_k_val = gate_top_k_val.view(-1, self.top_k)
        if self.preset_target and self.target_set_mode == 'soft':
            target_top_k_idx = self.preset_gate_top_k_idx

        gate_score = gate_top_k_val 

        """ Compute Guided Loss """
        if self.editing:
            init_expert_probs = torch.gather(input=gate, index=target_top_k_idx, dim=-1)
            guided_loss = -torch.log(init_expert_probs).sum() / gate.size(0)
            self.set_loss(guided_loss)

        if return_all_scores:
            return gate_top_k_idx, gate_score, gate
        return gate_top_k_idx, gate_score

    def set_target(self, target, set_mode):
        self.preset_target = True
        self.target_set_mode = set_mode
        self.preset_gate_top_k_idx = torch.stack([torch.where(t)[0] for t in target]).to(self.gate.weight.device) # [batch-size, top-k]
        
    def set_loss(self, loss):
        self.loss = loss

    def get_loss(self, clear=True):
        loss = self.loss
        if clear:
            self.loss = None
        return loss

    @property
    def has_loss(self):
        return self.loss is not None

    def set_result(self, gate_top_k_idx):
        k_hot_output = torch.zeros((gate_top_k_idx.shape[0], self.num_expert), dtype=torch.int32)
        k_hot_output.scatter_(1, gate_top_k_idx, 1)
        self.gate_score = k_hot_output


class _LoraExpert(nn.Module):
    r"""
    An expert using 2 FMoELinear modules to speed up the computation of experts
    within one worker.
    """

    def __init__(self, num_expert, d_model, d_hidden, activation, rank=0):
        super().__init__()
        self.htoh4 = nn.Linear(num_expert, d_model, d_hidden, bias=True, rank=rank)
        self.h4toh = nn.Linear(num_expert, d_hidden, d_model, bias=True, rank=rank)
        self.activation = activation

    def forward(self, inp, fwd_expert_count):
        r"""
        First expand input to 4h (the hidden size is variable, but is called h4
        for convenience). Then perform activation. Finally shirink back to h.
        """
        x = self.htoh4(inp, fwd_expert_count)
        x = self.activation(x)
        x = self.h4toh(x, fwd_expert_count)
        return x



class LoraLayer:
    def __init__(self, in_features: int, out_features: int, **kwargs):
        self.r = {}
        self.lora_alpha = {}
        self.scaling = {}
        self.lora_dropout = nn.ModuleDict({})
        self.lora_A = nn.ModuleDict({})
        self.lora_B = nn.ModuleDict({})
        # For Embedding layer
        self.lora_embedding_A = nn.ParameterDict({})
        self.lora_embedding_B = nn.ParameterDict({})
        self.nd_lora_A = dynamic()
        self.nd_lora_B = dynamic()


        # Mark the weight as unmerged
        self.merged = False
        self.disable_adapters = False
        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs

    def update_layer(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, num_rank_per_block):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if r > 0:
            """ 
            创建两个LoRA参数lora_A和lora_B。这些参数是通过nn.ParameterDict存储的，每个参数都使用nn.Parameter封装。参数的形状分别为(self.in_features, r)和(r, self.out_features)，其中r是低秩矩阵的秩，代表这些矩阵的列数（对于lora_A）和行数（对于lora_B）。初始状态下，这些参数被设置为零矩阵，使用self.weight.new_zeros方法确保它们和模型权重在同一个设备上。
            """
            self.lora_A = nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((self.in_features, r)))})
            self.lora_B = nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((r, self.out_features)))})
            self.nd_lora_A.update_dynamic(r, num_rank_per_block) # 设置maximun_rank，maximum_block等参数
            self.nd_lora_B.update_dynamic(r,num_rank_per_block)
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)

    def update_layer_conv2d(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if r > 0:
            kernel_size = self.kwargs["kernel_size"]
            stride = self.kwargs["stride"]
            padding = self.kwargs["padding"]
            self.lora_A.update(
                nn.ModuleDict({adapter_name: nn.Conv2d(self.in_features, r, kernel_size, stride, padding, bias=False)})
            )
            self.lora_B.update(
                nn.ModuleDict({adapter_name: nn.Conv2d(r, self.out_features, (1, 1), (1, 1), bias=False)})
            )
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)

    def update_layer_embedding(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # Actual trainable parameters
        if r > 0:
            self.lora_embedding_A.update(
                nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((r, self.in_features)))})
            )
            self.lora_embedding_B.update(
                nn.ParameterDict({adapter_name: nn.Parameter(self.weight.new_zeros((self.out_features, r)))})
            )
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)
        self.to(self.weight.device)

    def reset_lora_parameters(self, adapter_name):
        if adapter_name in self.lora_A.keys():
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A[adapter_name], a=math.sqrt(5))
            nn.init.zeros_(self.lora_B[adapter_name])
        if adapter_name in self.lora_embedding_A.keys():
            # initialize a the same way as the default for nn.linear and b to zero
            nn.init.zeros_(self.lora_embedding_A[adapter_name])
            nn.init.normal_(self.lora_embedding_B[adapter_name])


class RedundantLoraLayer(nn.Module):
    def __init__(self, in_features: int, out_features: int, **kwargs) -> None:
        super().__init__()


class ElderLoraLayer(nn.Module):
    def __init__(self, in_features: int, out_features: int, **kwargs):
        super(ElderLoraLayer, self).__init__()
        self.r = {}
        self.lora_alpha = {}
        self.scaling = {}
        self.lora_dropout = nn.ModuleDict({})
        self.lora_A = nn.ModuleDict({})
        self.lora_B = nn.ModuleDict({})

        # Mark the weight as unmerged
        self.merged = False
        self.disable_adapters = False
        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs

    def update_layer(self, adapter_name, r, lora_alpha, lora_dropout, init_lora_weights):
        self.r[adapter_name] = r
        self.lora_alpha[adapter_name] = lora_alpha
        if lora_dropout > 0.0:
            lora_dropout_layer = nn.Dropout(p=lora_dropout)
        else:
            lora_dropout_layer = nn.Identity()

        self.lora_dropout.update(nn.ModuleDict({adapter_name: lora_dropout_layer}))
        # lora
        if r > 0:
            self.lora_A = nn.ParameterDict({adapter_name: nn.Parameter(torch.zeros((self.in_features, r)))})
            self.lora_B = nn.ParameterDict({adapter_name: nn.Parameter(torch.zeros((r, self.out_features)))})
            self.scaling[adapter_name] = lora_alpha / r
        if init_lora_weights:
            self.reset_lora_parameters(adapter_name)

    def reset_lora_parameters(self, adapter_name):
        if adapter_name in self.lora_A.keys():
            # initialize A the same way as the default for nn.Linear and B to zero
            nn.init.kaiming_uniform_(self.lora_A[adapter_name], a=math.sqrt(5))
            nn.init.zeros_(self.lora_B[adapter_name])


class ElderLinear(nn.Linear):
    # Lora implemented in a dense layer
    def __init__(
        self,
        adapter_name: str,
        in_features: int,
        out_features: int,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        num_experts: int = 4,
        top_k: int = 2,
        gate_bias: bool = True,
        dropout: float = 0.0,
        bias: bool = True,
        is_redundant_experts: bool = False,
        **kwargs,
    ):
        init_lora_weights = kwargs.pop("init_lora_weights", True)
        nn.Linear.__init__(self, in_features=in_features, out_features=out_features, **kwargs)

        # Freezing the pre-trained weight matrix
        self.weight.requires_grad = False

        self.fan_in_fan_out = fan_in_fan_out
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

        nn.Linear.reset_parameters(self)
        self.active_adapter = adapter_name
        self.disable_adapters = False
        self.key_idx = -1

        """ Add parameters for ELDER adapter """
        def one_expert(d_model):
            expert_func = ElderLoraLayer(d_model, out_features)
            expert_func.update_layer(self.active_adapter, r, lora_alpha, lora_dropout, init_lora_weights)
            return expert_func
        expert = one_expert
        self.num_experts = num_experts
        
        self.gate = Gate(in_features, num_experts, top_k, gate_bias=gate_bias)
        self.experts = nn.ModuleList([expert(in_features) for _ in range(num_experts)])

    def merge(self):
        """ 
        Merge LoRA Weights
        """
        if self.active_adapter not in self.lora_A.keys():
            return
        if self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data += (
                transpose(
                    self.lora_B[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = True

    def unmerge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data -= (
                transpose(
                    self.lora_B[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = False

    def forward(self, x: torch.Tensor):
        previous_dtype = x.dtype
        result = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        """ 
        First computes gate output, 
        then conduct MoE forward according to the gate 
        """
        if self.disable_adapters:
            return result

        original_shape = x.shape # [B, L, H]
        batch_size = original_shape[0]
        seq_length = original_shape[1]

        """ take representations from GRACE layer for routing """
        batch_query = SEQ_REPR
        gate_top_k_idx, gate_score = self.gate(batch_query)
        full_gate_weights = torch.zeros((batch_size, self.num_experts), device=x.device) # [B, n-experts]
        full_gate_weights.scatter_(dim=1, index=gate_top_k_idx, src=gate_score)

        expert_outputs = torch.stack([
            (e.lora_dropout[self.active_adapter](x) @ e.lora_A[self.active_adapter] @ e.lora_B[self.active_adapter])\
                 * e.scaling[self.active_adapter] for e in self.experts
        ], dim=1)
        lora_result = torch.einsum('belh, be -> blh', expert_outputs, full_gate_weights)

        """ Make sure deferral scope """
        flag = IN_EDIT_SCOPE # condition list
        condition_tensor = torch.tensor(flag).view(len(flag), 1, 1).to(result.device)
        result_tensor = torch.where(condition_tensor, result + lora_result, result) # apply lora only to date in the edit scope

        self.gate_loss = self.gate.loss

        result_tensor = result_tensor.to(previous_dtype)

        return result_tensor


class ElderGraceLinear(nn.Linear, GraceLayer):
    def __init__(
            self,
            adapter_name: str,
            in_features: int,
            out_features: int,
            grace_config: dict,
            gate_linears: list,
            top_k: int,
            num_experts: int,
            fan_in_fan_out: bool = False, # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
            **kwargs
    ):
        nn.Linear.__init__(self, in_features, out_features,**kwargs)
        GraceLayer.__init__(self,grace_config=grace_config, in_features=in_features, out_features=out_features)
        self.fan_in_fan_out = fan_in_fan_out
        self.gate_linear_layers = gate_linears
        self.num_experts = num_experts
        self.top_k = min(top_k, num_experts)
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

    def get_bin_code(self, batch_query):
        """ when this is the last_iter, record bin_code """
        with torch.no_grad():
            k_hot_outputs = []
            for linear in self.gate_linear_layers:
                gate = linear(batch_query).detach()
                _, gate_top_k_idx = torch.topk(
                    gate, k=self.top_k, dim=-1, largest=True, sorted=False
                )  # [.. x top_k]
                k_hot_outputs.append(torch.zeros((gate_top_k_idx.shape[0], self.num_experts), dtype=torch.int32, device=gate.device).scatter_(1, gate_top_k_idx, 1))
            gate_output_concat = torch.cat(k_hot_outputs, dim=1) # [B, L*E] L:n-layers
            self.binary_code = gate_output_concat

    def discriminate(self, threshold):
        binary_code_dataset_bool = torch.cat(BINARY_CODE, dim=0).to(torch.bool)
        expanded_self_binary_code_bool = self.binary_code.to(torch.bool).unsqueeze(1)
        xor_result = expanded_self_binary_code_bool ^ binary_code_dataset_bool
        hamming_distance = xor_result.sum(dim=2)
        distances_min = hamming_distance.min(dim=1)[0].tolist()

        flag = [x < threshold for x in distances_min]
        return flag

    def set_discrimination_anchor(self, hash_initialization):
        binary_code_dataset_bool = torch.stack([torch.cat(value, dim=0) for value in hash_initialization.values()]) # [num-class, length-of-code]
        self.discriminate_anchor = binary_code_dataset_bool.to(self.weight.device)
        self.hash_init_as_anchor = True


    def forward(self, x: torch.Tensor):
        global SEQ_REPR
        global IN_EDIT_SCOPE

        layer_out = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        if self.disable_grace:
            return layer_out

        if isinstance(self.key_id, torch.Tensor):
            batch_query = torch.gather(x, 1, self.key_id.unsqueeze(1).unsqueeze(1).expand(-1, -1, x.shape[2])).squeeze(1).detach()
        elif self.key_id == -1: 
            batch_query = x[:, self.key_id].detach()
        else:
            raise NotImplementedError
        SEQ_REPR = batch_query

        if hasattr(self, 'is_last_iter'):
            if self.is_last_iter:
                self.get_bin_code(batch_query)
                BINARY_CODE.append(self.binary_code)

        """ Inference time. Deferral Mechanism """
        if hasattr(self, 'editing'):
            if not self.editing:
                self.get_bin_code(batch_query)
                IN_EDIT_SCOPE = self.discriminate(threshold=self.threshold)
            else:
                IN_EDIT_SCOPE = [True for _ in range(x.shape[0])]
        
        return layer_out


class Linear(nn.Linear, LoraLayer):
    # Lora implemented in a dense layer
    def __init__(
        self,
        adapter_name: str,
        in_features: int,
        out_features: int,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        num_rank_per_block: int = 1,
        **kwargs,
    ):
        init_lora_weights = kwargs.pop("init_lora_weights", True)

        nn.Linear.__init__(self, in_features, out_features, **kwargs)
        LoraLayer.__init__(self, in_features=in_features, out_features=out_features)
        # Freezing the pre-trained weight matrix
        self.weight.requires_grad = False

        self.fan_in_fan_out = fan_in_fan_out
        if fan_in_fan_out:
            self.weight.data = self.weight.data.T

        nn.Linear.reset_parameters(self)
        self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights, num_rank_per_block)
        self.lora_block_mapping = LORA_BLOCK_MAPPING
        self.active_adapter = adapter_name

    def merge(self):
        """ 
        Merge LoRA weights with Linear weights. Not applicable to Mix-LoRA
        """
        if self.active_adapter not in self.lora_A.keys():
            return
        if self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data += (
                transpose(
                    self.lora_B[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = True

    def unmerge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data -= (
                transpose(
                    self.lora_B[self.active_adapter].weight @ self.lora_A[self.active_adapter].weight,
                    self.fan_in_fan_out,
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = False

    def forward(self, x: torch.Tensor):
        previous_dtype = x.dtype
        if self.active_adapter not in self.lora_A.keys():
            return F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        if self.disable_adapters:
            if self.r[self.active_adapter] > 0 and self.merged:
                self.unmerge()
            result = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
        elif self.r[self.active_adapter] > 0 and not self.merged:
            result = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)
            lora_A = self.nd_lora_A(self.lora_A[self.active_adapter].T).mT
            lora_B = self.nd_lora_B(self.lora_B[self.active_adapter])
            result += (self.lora_dropout[self.active_adapter](x) @ lora_A @ lora_B) \
                      * self.scaling[self.active_adapter]

        else:
            result = F.linear(x, transpose(self.weight, self.fan_in_fan_out), bias=self.bias)

        result = result.to(previous_dtype)

        return result

class Embedding(nn.Embedding, LoraLayer):
    # LoRA implemented in a Embedding layer
    def __init__(
        self,
        adapter_name: str,
        num_embeddings: int,
        embedding_dim: int,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        **kwargs,
    ):
        init_lora_weights = kwargs.pop("init_lora_weights", True)

        nn.Embedding.__init__(self, num_embeddings, embedding_dim, **kwargs)
        LoraLayer.__init__(self, in_features=num_embeddings, out_features=embedding_dim)

        self.weight.requires_grad = False

        nn.Embedding.reset_parameters(self)
        self.update_layer_embedding(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
        self.active_adapter = adapter_name

    def unmerge(self, mode: bool = True):
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data -= (
                transpose(
                    self.lora_embedding_B[self.active_adapter] @ self.lora_embedding_A[self.active_adapter], True
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = False

    def merge(self):
        if self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            self.weight.data += (
                transpose(
                    self.lora_embedding_B[self.active_adapter] @ self.lora_embedding_A[self.active_adapter], True
                )
                * self.scaling[self.active_adapter]
            )
            self.merged = True

    def forward(self, x: torch.Tensor):
        if self.disable_adapters:
            if self.r[self.active.adapter] > 0 and self.merged:
                self.weight.data -= (
                    transpose(
                        self.lora_embedding_B[self.active_adapter].weight
                        @ self.lora_embedding_A[self.active_adapter].weight,
                        True,
                    )
                    * self.scaling[self.active_adapter]
                )
                self.merged = False
            return nn.Embedding.forward(self, x)

        elif self.r[self.active_adapter] > 0 and not self.merged:
            result = nn.Embedding.forward(self, x)
            if self.r[self.active_adapter] > 0:
                after_A = F.embedding(
                    x,
                    self.lora_embedding_A[self.active_adapter].T,
                    self.padding_idx,
                    self.max_norm,
                    self.norm_type,
                    self.scale_grad_by_freq,
                    self.sparse,
                )
                result += (after_A @ self.lora_embedding_B[self.active_adapter].T) * self.scaling[self.active_adapter]
            return result
        else:
            return nn.Embedding.forward(self, x)


class Conv2d(nn.Conv2d, LoraLayer):
    # Lora implemented in a conv2d layer
    def __init__(
        self,
        adapter_name: str,
        in_channels: int,
        out_channels: int,
        kernel_size: Union[int, Tuple[int]],
        stride: Union[int, Tuple[int]] = 1,
        padding: Union[int, Tuple[int]] = 0,
        r: int = 0,
        lora_alpha: int = 1,
        lora_dropout: float = 0.0,
        **kwargs,
    ):
        init_lora_weights = kwargs.pop("init_lora_weights", True)

        nn.Conv2d.__init__(self, in_channels, out_channels, kernel_size, stride, padding)
        LoraLayer.__init__(
            self,
            in_features=in_channels,
            out_features=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
        )
        # Freezing the pre-trained weight matrix
        self.weight.requires_grad = False

        nn.Conv2d.reset_parameters(self)
        self.update_layer_conv2d(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
        self.active_adapter = adapter_name

    def merge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            # https://github.com/bmaltais/kohya_ss/blob/feb6728762a8f463d15ba936d189d4c3abfaa1ab/networks/lora.py#L117
            if self.weight.size()[2:4] == (1, 1):
                # conv2d 1x1
                self.weight.data += (
                    self.lora_B[self.active_adapter].weight.squeeze(3).squeeze(2)
                    @ self.lora_A[self.active_adapter].weight.squeeze(3).squeeze(2)
                ).unsqueeze(2).unsqueeze(3) * self.scaling[self.active_adapter]
            else:
                # conv2d 3x3
                self.weight.data += (
                    F.conv2d(
                        self.lora_A[self.active_adapter].weight.permute(1, 0, 2, 3),
                        self.lora_B[self.active_adapter].weight,
                    ).permute(1, 0, 2, 3)
                    * self.scaling[self.active_adapter]
                )
            self.merged = True

    def unmerge(self):
        if self.active_adapter not in self.lora_A.keys():
            return
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        if self.r[self.active_adapter] > 0:
            if self.weight.size()[2:4] == (1, 1):
                # conv2d 1x1
                self.weight.data -= (
                    self.lora_B[self.active_adapter].weight.squeeze(3).squeeze(2)
                    @ self.lora_A[self.active_adapter].weight.squeeze(3).squeeze(2)
                ).unsqueeze(2).unsqueeze(3) * self.scaling[self.active_adapter]
            else:
                # conv2d 3x3
                self.weight.data += (
                    F.conv2d(
                        self.lora_A[self.active_adapter].weight.permute(1, 0, 2, 3),
                        self.lora_B[self.active_adapter].weight,
                    ).permute(1, 0, 2, 3)
                    * self.scaling[self.active_adapter]
                )
            self.merged = False

    def forward(self, x: torch.Tensor):
        previous_dtype = x.dtype

        if self.active_adapter not in self.lora_A.keys():
            return F.conv2d(
                x,
                self.weight,
                bias=self.bias,
                stride=self.stride,
                padding=self.padding,
                dilation=self.dilation,
                groups=self.groups,
            )
        if self.disable_adapters:
            if self.r[self.active_adapter] > 0 and self.merged:
                self.unmerge()
            result = F.conv2d(
                x,
                self.weight,
                bias=self.bias,
                stride=self.stride,
                padding=self.padding,
                dilation=self.dilation,
                groups=self.groups,
            )
        elif self.r[self.active_adapter] > 0 and not self.merged:
            result = F.conv2d(
                x,
                self.weight,
                bias=self.bias,
                stride=self.stride,
                padding=self.padding,
                dilation=self.dilation,
                groups=self.groups,
            )

            x = x.to(self.lora_A[self.active_adapter].weight.dtype)

            result += (
                self.lora_B[self.active_adapter](
                    self.lora_A[self.active_adapter](self.lora_dropout[self.active_adapter](x))
                )
                * self.scaling[self.active_adapter]
            )
        else:
            result = F.conv2d(
                x,
                self.weight,
                bias=self.bias,
                stride=self.stride,
                padding=self.padding,
                dilation=self.dilation,
                groups=self.groups,
            )

        result = result.to(previous_dtype)

        return result


if is_bnb_available():

    class Linear8bitLt(bnb.nn.Linear8bitLt, LoraLayer):
        # Lora implemented in a dense layer
        def __init__(
            self,
            adapter_name,
            in_features,
            out_features,
            r: int = 0,
            lora_alpha: int = 1,
            lora_dropout: float = 0.0,
            **kwargs,
        ):
            bnb.nn.Linear8bitLt.__init__(
                self,
                in_features,
                out_features,
                bias=kwargs.get("bias", True),
                has_fp16_weights=kwargs.get("has_fp16_weights", True),
                memory_efficient_backward=kwargs.get("memory_efficient_backward", False),
                threshold=kwargs.get("threshold", 0.0),
                index=kwargs.get("index", None),
            )
            LoraLayer.__init__(self, in_features=in_features, out_features=out_features)

            # Freezing the pre-trained weight matrix
            self.weight.requires_grad = False
            init_lora_weights = kwargs.pop("init_lora_weights", True)
            self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
            self.active_adapter = adapter_name

        def forward(self, x: torch.Tensor):
            result = super().forward(x)

            if self.disable_adapters or self.active_adapter not in self.lora_A.keys():
                return result
            elif self.r[self.active_adapter] > 0:
                if not torch.is_autocast_enabled():
                    expected_dtype = result.dtype

                    if x.dtype != torch.float32:
                        x = x.float()
                    output = (
                        self.lora_B[self.active_adapter](
                            self.lora_A[self.active_adapter](self.lora_dropout[self.active_adapter](x))
                        ).to(expected_dtype)
                        * self.scaling[self.active_adapter]
                    )
                else:
                    output = (
                        self.lora_B[self.active_adapter](
                            self.lora_A[self.active_adapter](self.lora_dropout[self.active_adapter](x))
                        )
                        * self.scaling[self.active_adapter]
                    )
                result += output
            return result

    if is_bnb_4bit_available():

        class Linear4bit(bnb.nn.Linear4bit, LoraLayer):
            # Lora implemented in a dense layer
            def __init__(
                self,
                adapter_name,
                in_features,
                out_features,
                r: int = 0,
                lora_alpha: int = 1,
                lora_dropout: float = 0.0,
                **kwargs,
            ):
                bnb.nn.Linear4bit.__init__(
                    self,
                    in_features,
                    out_features,
                    bias=kwargs.get("bias", True),
                    compute_dtype=kwargs.get("compute_dtype", torch.float32),
                    compress_statistics=kwargs.get("compress_statistics", True),
                    quant_type=kwargs.get("quant_type", "nf4"),
                )
                LoraLayer.__init__(self, in_features=in_features, out_features=out_features)

                # Freezing the pre-trained weight matrix
                self.weight.requires_grad = False

                init_lora_weights = kwargs.pop("init_lora_weights", True)
                self.update_layer(adapter_name, r, lora_alpha, lora_dropout, init_lora_weights)
                self.active_adapter = adapter_name

            def forward(self, x: torch.Tensor):
                result = super().forward(x)

                if self.disable_adapters or self.active_adapter not in self.lora_A.keys():
                    return result
                elif self.r[self.active_adapter] > 0:
                    result = result.clone()
                    if not torch.is_autocast_enabled():
                        expected_dtype = result.dtype
                        x = x.to(self.lora_A[self.active_adapter].weight.dtype)
                        output = (
                            self.lora_B[self.active_adapter](
                                self.lora_A[self.active_adapter](self.lora_dropout[self.active_adapter](x))
                            ).to(expected_dtype)
                            * self.scaling[self.active_adapter]
                        )
                    else:
                        output = (
                            self.lora_B[self.active_adapter](
                                self.lora_A[self.active_adapter](self.lora_dropout[self.active_adapter](x))
                            )
                            * self.scaling[self.active_adapter]
                        )
                    result += output
                return result


