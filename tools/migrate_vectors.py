import os
import sys
import argparse
import sqlite3
import json
import logging
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("migrate_vectors")

def main():
    parser = argparse.ArgumentParser(description="Migrate missing embeddings for existing memories.")
    parser.add_argument("--db", type=str, default="data/tmemory.db", help="Path to tmemory.db")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for generating embeddings")
    args = parser.parse_args()

    db_path = args.db
    if not os.path.exists(db_path):
        logger.error(f"Database file not found: {db_path}")
        sys.exit(1)

    try:
        import sqlite_vec
    except ImportError:
        logger.error("sqlite_vec not installed. Run: pip install sqlite-vec")
        sys.exit(1)

    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        import numpy as np
    except ImportError:
        logger.error("onnxruntime or tokenizers not installed. Run: pip install onnxruntime tokenizers numpy")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    
    # 查找尚未嵌入的记忆
    cursor = conn.cursor()
    cursor.execute("""
        SELECT m.id, m.memory 
        FROM memories m 
        LEFT JOIN memory_vectors v ON m.id = v.memory_id 
        WHERE v.memory_id IS NULL AND m.is_active = 1
    """)
    rows = cursor.fetchall()
    
    if not rows:
        logger.info("No active memories found requiring vector migration.")
        sys.exit(0)

    logger.info(f"Found {len(rows)} memories needing embeddings. Loading local embedding model...")
    
    model_dir = "data/bge-small-zh-v1.5" # 后面需要写下载脚本
    if not os.path.exists(model_dir):
        logger.error(f"Model directory not found: {model_dir}. Need to download the model first.")
        sys.exit(1)

    try:
        tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        session = ort.InferenceSession(os.path.join(model_dir, "model_quantized.onnx"))
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        sys.exit(1)

    def get_embedding(text):
        encoding = tokenizer.encode(text)
        inputs = {
            "input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([encoding.type_ids], dtype=np.int64)
        }
        outputs = session.run(None, inputs)
        # 取 [CLS] token (index 0) 的输出
        embedding = outputs[0][0, 0, :]
        # L2 归一化
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding.tolist()

    logger.info("Starting migration...")
    
    inserted = 0
    with tqdm(total=len(rows)) as pbar:
        for row in rows:
            mem_id = row['id']
            text = row['memory']
            try:
                emb = get_embedding(text)
                # sqlite-vec 使用 vector/float 数组的特殊序列化
                import struct
                # sqlite_vec 通常可以将二进制传递给 vec0 表
                emb_bytes = struct.pack(f"{len(emb)}f", *emb)
                
                cursor.execute(
                    "INSERT INTO memory_vectors(memory_id, embedding) VALUES (?, ?)", 
                    (mem_id, emb_bytes)
                )
                conn.commit()
                inserted += 1
            except Exception as e:
                logger.error(f"Failed to embed memory {mem_id}: {e}")
            pbar.update(1)

    logger.info(f"Migration completed. Successfully embedded {inserted}/{len(rows)} memories.")

if __name__ == "__main__":
    main()
