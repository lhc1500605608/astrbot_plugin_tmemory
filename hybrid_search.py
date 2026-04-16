import sqlite3
import json
import jieba
import logging
import numpy as np
from typing import List, Dict, Any, Optional

logger = logging.getLogger("HybridSearch")

class SQLiteVecKNNRetriever:
    """基于 sqlite-vec 的 KNN 向量检索"""
    def __init__(self, conn: sqlite3.Connection, vector_dim: int, table_name: str = "memory_vectors"):
        self.conn = conn
        self.vector_dim = vector_dim
        self.table_vec = table_name

    def _serialize_vector(self, vector: List[float]) -> bytes:
        return np.array(vector, dtype=np.float32).tobytes()

    def search_knn(self, query_vector: List[float], top_k: int = 5) -> List[Dict[str, Any]]:
        vec_bytes = self._serialize_vector(query_vector)
        try:
            cursor = self.conn.cursor()
            query = f"""
                SELECT memory_id, distance
                FROM {self.table_vec}
                WHERE embedding MATCH ? AND k = ?
                ORDER BY distance
            """
            cursor.execute(query, (vec_bytes, top_k))
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                results.append({
                    "id": row["memory_id"],
                    "score": 1.0 / (1.0 + float(row["distance"])),
                    "distance": float(row["distance"])
                })
            return results
        except Exception as e:
            logger.debug(f"[tmemory] vector query failed: {e}")
            return []


class FTSMemoryDB:
    """基于 SQLite FTS5 的全文检索"""
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _tokenize(self, text: str) -> str:
        if not text:
            return ""
        tokens = jieba.cut_for_search(text)
        return " ".join(tokens)

    def search_fts(self, query: str, canonical_user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        query_tokens = list(jieba.cut_for_search(query))
        fts_query = " AND ".join(query_tokens)
        if not fts_query:
            return []

        sql = """
            SELECT rowid, rank
            FROM memories_fts
            WHERE memories_fts MATCH ? AND canonical_user_id = ?
            ORDER BY rank LIMIT ?
        """
        params = [fts_query, canonical_user_id]

        try:
            cursor = self.conn.cursor()
            cursor.execute(sql, params)
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "id": row["rowid"],
                    "score": abs(row["rank"]),
                    "rank": row["rank"]
                })
            return results
        except Exception as e:
            logger.debug(f"[tmemory] fts query failed: {e}")
            return []


class RRFSearchFusion:
    """Reciprocal Rank Fusion (RRF) 算法实现"""
    def __init__(self, k: int = 60):
        self.k = k

    def fuse(self, vector_results: List[Dict], fts_results: List[Dict], top_k: int = 5) -> List[Dict]:
        rrf_scores = {}
        
        for rank, item in enumerate(vector_results, 1):
            doc_id = item["id"]
            rrf_score = 1.0 / (self.k + rank)
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = {"id": doc_id, "rrf_score": 0.0, "sources": []}
            rrf_scores[doc_id]["rrf_score"] += rrf_score
            rrf_scores[doc_id]["sources"].append("vector")

        for rank, item in enumerate(fts_results, 1):
            doc_id = item["id"]
            rrf_score = 1.0 / (self.k + rank)
            if doc_id not in rrf_scores:
                rrf_scores[doc_id] = {"id": doc_id, "rrf_score": 0.0, "sources": []}
            rrf_scores[doc_id]["rrf_score"] += rrf_score
            rrf_scores[doc_id]["sources"].append("fts")

        fused_results = list(rrf_scores.values())
        fused_results.sort(key=lambda x: x["rrf_score"], reverse=True)
        return fused_results[:top_k]


class HybridMemorySystem:
    """混合检索内存系统集成"""
    def __init__(self, conn: sqlite3.Connection, vector_dim: int):
        self.conn = conn
        self.vector_dim = vector_dim
        
        self.knn_retriever = SQLiteVecKNNRetriever(self.conn, vector_dim, table_name="memory_vectors")
        self.fts_db = FTSMemoryDB(self.conn)
        self.rrf_fusion = RRFSearchFusion(k=60)

    def hybrid_search(self, query: str, query_vector: Optional[List[float]], canonical_user_id: str, top_k: int = 80, recall_ratio: int = 1) -> List[Dict]:
        """融合检索：先分别召回，再RRF重排。"""
        recall_k = top_k * recall_ratio
        
        vec_results = []
        if query_vector:
            vec_results = self.knn_retriever.search_knn(query_vector, top_k=recall_k)
            
        fts_results = []
        if query:
            fts_results = self.fts_db.search_fts(query, canonical_user_id=canonical_user_id, limit=recall_k)
            
        fused = self.rrf_fusion.fuse(vec_results, fts_results, top_k=top_k)
        
        # 归一化 RRF 分数到 0~1 方便后续加权
        if fused:
            max_rrf = max(item["rrf_score"] for item in fused)
            if max_rrf > 0:
                for item in fused:
                    item["rrf_score"] /= max_rrf
                    
        return fused
