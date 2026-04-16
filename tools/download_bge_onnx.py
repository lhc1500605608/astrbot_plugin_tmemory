import os
import sys
import logging
from huggingface_hub import snapshot_download

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("download_model")

def main():
    repo_id = "Xenova/bge-small-zh-v1.5"
    save_dir = "data/bge-small-zh-v1.5"
    
    if os.path.exists(os.path.join(save_dir, "model_quantized.onnx")):
        logger.info(f"Model already downloaded at {save_dir}")
        sys.exit(0)
        
    logger.info(f"Downloading {repo_id} from HuggingFace to {save_dir}...")
    try:
        # 使用国内镜像加速下载
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        # 下载 onnx 模型和 tokenizer 文件
        snapshot_download(
            repo_id=repo_id,
            local_dir=save_dir,
            allow_patterns=["*.onnx", "*.json", "*.txt"],
            local_dir_use_symlinks=False
        )
        logger.info("Download completed successfully!")
    except Exception as e:
        logger.error(f"Failed to download model: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
