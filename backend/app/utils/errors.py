class AudioProcessingError(Exception):
    """音频预处理失败 (解码/重采样/参数不兼容)。映射为 HTTP 400。"""


class EngineNotReadyError(Exception):
    """推理引擎尚未加载就绪。映射为 HTTP 503。"""


class UnsupportedParameterError(AudioProcessingError):
    """输入参数无法适配到模型契约。映射为 HTTP 400。"""
