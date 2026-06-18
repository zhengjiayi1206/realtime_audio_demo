"""
gxl_ai_utils 的轻量 mock，提供 Easy-Turn 模型推理所需的最小接口。
"""

import os
import logging
import yaml

logger = logging.getLogger(__name__)

# 限频打印缓存
_limit_cache = {}


def logging_limit_print(msg, limit=1):
    """限制打印次数 (mock 实现, 等同于 logging.info)。"""
    key = str(msg)[:100]
    count = _limit_cache.get(key, 0)
    if count < limit:
        logger.info(msg)
        _limit_cache[key] = count + 1


def logging_info(msg):
    logger.info(msg)


def logging_error(msg):
    logger.error(msg)


def load_dict_from_yaml(path):
    """从 YAML 文件加载字典。"""
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def load_list_file_clean(path):
    """加载列表文件, 去除空行。"""
    with open(path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]


def write_list_to_file(data, path):
    """将列表写入文件。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        for item in data:
            f.write(str(item) + '\n')


def load_first_row_clean(path):
    """加载文件的第一行非空内容。"""
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                return line
    return ""


def makedir_for_file(filepath):
    """为文件路径创建父目录。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)


def do_get_file_pure_name_from_path(path):
    """获取不带扩展名的文件名。"""
    return os.path.splitext(os.path.basename(path))[0]


def print_model_size(model, name=""):
    """打印模型参数量。"""
    total = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info(f"[{name}] 参数: {total/1e6:.1f}M total, {trainable/1e6:.1f}M trainable")
