# Copyright 2025 Emcie Co Ltd.
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

from openai import AsyncClient

from typing_extensions import override
import os

import parlant.adapters.nlp.openai_service as op
from parlant.core.loggers import Logger
from parlant.core.tracer import Tracer
from parlant.core.meter import Meter
from parlant.core.nlp.tokenization import EstimatingTokenizer
from parlant.adapters.nlp.openai_service import OpenAIEmbedder
from parlant.core.nlp.embedding import Embedder
from lagom import Container
from parlant.core.nlp.moderation import ModerationService, NoModeration

# 创建一个用于阿里云的估算分词器
class AliyunEstimatingTokenizer(EstimatingTokenizer):
    def __init__(self, model_name: str = "text-embedding-v4") -> None:
        self.model_name = model_name

    @override
    async def estimate_token_count(self, prompt: str) -> int:
        """使用阿里云官方的token计数方式"""
        try:
            from dashscope import Tokenization
            resp = Tokenization.call(model=self.model_name, prompt=prompt)
            return len(resp.output['tokens'])
        except Exception:
            # 回退方案：近似估算
            return int(len(prompt) / 2.5)


# 创建具体的阿里云文本嵌入器类
class AliyunTextEmbedding4(OpenAIEmbedder):
    def __init__(self, logger: Logger, tracer: Tracer, meter: Meter) -> None:
        self._logger = logger
        self._tracer = tracer
        self._meter = meter
        self.model_name = "text-embedding-v4"
        self._tokenizer = AliyunEstimatingTokenizer(model_name=self.model_name)

    @property
    @override
    def max_tokens(self) -> int:
        return 8192

    @property
    def dimensions(self) -> int:
        return 1024


class CustomAliyunService(op.OpenAIService):

    @override
    async def get_embedder(self) -> Embedder:
        return AliyunTextEmbedding4(logger=self._logger, tracer=self._tracer, meter=self._meter)

    @override
    async def get_moderation_service(self) -> ModerationService:
        return NoModeration()

def DeepseekLLM_aliyun_Embed(container: Container):
    if error := CustomAliyunService.verify_environment():
        raise error
    return CustomAliyunService(container[Logger], container[Tracer], container[Meter])
