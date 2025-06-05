import pandas as pd
import numpy as np
import time
import os
import unicodedata
import re
from tqdm import tqdm
from collections import defaultdict, Counter
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import logging

# Cấu hình logging
logging.basicConfig(level=logging.DEBUG,format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

Version = "0.1.0"
CSV_PATH = "input/sample_ocr_human_value_kiengiang_khaisinh.csv"
FIELD_FILTER = None

def normalize_text(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

class TfidfModel:
    def __init__(self):
        self.THRESHOLD = 0.85
        self.MIN_OCCURRENCE =2
        self.human_memory = defaultdict(list)
        self.human_memory_set = defaultdict(set)
        self.human_counter = defaultdict(lambda: defaultdict(int))  # human_value counter
        self.vectorizers = {}
        self.vector_matrices = {}
        self.lookup_dict = defaultdict(dict)  # field -> ocr_value -> last human_value

    def learn_if_valid(self, field, ocr_value, human_value, suggestion):
        if not human_value:
            return

        # update lookup_dict
        self.lookup_dict[field][ocr_value] = human_value

        # update human_counter
        self.human_counter[field][human_value] += 1

        # Chỉ học nếu human_value lặp lại >= MIN_OCCURRENCE và chưa học, và suggest != human
        if (
            self.human_counter[field][human_value] >= self.MIN_OCCURRENCE
            and human_value not in self.human_memory_set[field]
            and suggestion != human_value
        ):
            self.human_memory[field].append(human_value)
            self.human_memory_set[field].add(human_value)
            self._update_vectorizer(field)

    def _update_vectorizer(self, field):
        vectorizer = TfidfVectorizer(ngram_range=(1, 2))
        mat = vectorizer.fit_transform(self.human_memory[field])
        self.vectorizers[field] = vectorizer
        self.vector_matrices[field] = mat

    def suggest(self, field, ocr_value):
        suggestion = None
        confidence = 0.0

        if not self.human_memory[field]:
            return suggestion, confidence
        
        vec = self.vectorizers[field].transform([ocr_value])
        sim = cosine_similarity(vec, self.vector_matrices[field])[0]
        best_idx = np.argmax(sim)
        best_score = sim[best_idx]
        best_score = round(float(best_score), 4)
        suggestion = self.human_memory[field][best_idx]

        if best_score >=self.THRESHOLD:
            logger.debug(f"✅  High confidence for field '{field}' with OCR value '{ocr_value}': {best_score} - suggestion value: {suggestion}")
            suggestion = self.human_memory[field][best_idx]
            confidence = best_score
        else:          # Fallback if confidence < THRESHOLD
            logger.debug(f"⚠️  Low confidence for field '{field}' with OCR value '{ocr_value}': {best_score} - suggestion value: {suggestion}. Using fallback.")
            
            # reset suggestion and confidence
            suggestion = None
            confidence = 0.0
            
            fallback = self.fallback_lookup(field, ocr_value)
            if fallback:
                suggestion = fallback
                confidence = 1.0  # Fallback alway has  confidence = 1.0
                logger.debug(f"🔄 Fallback suggestion for field '{field}' with OCR value '{ocr_value}': {suggestion}")
            else:
                suggestion = None
                confidence = 0.0

        return suggestion, confidence

    def fallback_lookup(self, field, ocr_value):
        return self.lookup_dict[field].get(ocr_value, None)

def run_replay(csv_path):
    df = pd.read_csv(csv_path)
    df = df.dropna(subset=["doctypefieldcode", "ocr_value", "human_value"])
    df["ocr_value"] = df["ocr_value"].astype(str).str.strip()
    df["human_value"] = df["human_value"].astype(str).str.strip()

    if FIELD_FILTER:
        df = df[df["doctypefieldcode"] == FIELD_FILTER].reset_index(drop=True)

    tfidf_fixer = TfidfModel()
    df["ocr_correct"] = df["ocr_value"].str.strip() == df["human_value"].str.strip()
    df["tfidf_predict"] = ""
    df["tfidf_confidence"] = 0.0
    df["tfidf_correct"] = False
    df["tfidf_suggested"] = False
    df["tfidf_final_correct"] = False
    df["predict_time_ms"] = 0.0
    df["predict_confidence"] =0.0
    df["predict_nagative"] = False
    logger.info("Đang chạy TF-IDF Replay...")
    for idx, row in tqdm(df.iterrows(), total=len(df), desc="🔁 Replay", unit="record"):
        try:
            field = row["doctypefieldcode"]
            ocr =normalize_text(row["ocr_value"])
            human = normalize_text(row["human_value"])

            start = time.time()
            suggestion, confidence = tfidf_fixer.suggest(field, ocr)

            elapsed = (time.time() - start) * 1000

            df.at[idx, "tfidf_predict"] = suggestion if suggestion else ""
            df.at[idx, "tfidf_confidence"] = confidence
            df.at[idx, "tfidf_suggested"] = bool(suggestion)
            df.at[idx, "tfidf_correct"] = (suggestion == human or suggestion == "")
            df.at[idx, "tfidf_final_correct"] = (suggestion == human) if suggestion else (ocr == human)
            df.at[idx, "predict_time_ms"] = elapsed
            df.at[idx, "predict_confidence"] = confidence
            df.at[idx, "predict_nagative"] = (bool(suggestion) and human==ocr and suggestion != human)
            
            tfidf_fixer.learn_if_valid(field, ocr, human, suggestion)

        except Exception as e:
            logging.error(f"⚠️  Error at row {idx}: {e}")

    logger.info("Đang tổng hợp kết quả...")
    summary = (
        df.groupby("doctypefieldcode")
        .agg(
            total_records=("ocr_value", "count"),
            correct_ocr=("ocr_correct", "sum"),
            tfidf_predicted=("tfidf_suggested", "sum"),
            tfidf_predicted_correct=("tfidf_correct", "sum"),
            tfidf_final_correct=("tfidf_final_correct", "sum"),
            predict_nagative=("predict_nagative", "sum"),
            min_time_ms=("predict_time_ms", "min"),
            max_time_ms=("predict_time_ms", "max"),
            avg_time_ms=("predict_time_ms", "mean"),
            min_confidence=("predict_confidence", "min"),
            max_confidence=("predict_confidence", "max"),
            avg_confidence=("predict_confidence", "mean")
        )
        .reset_index()
    )

    summary["incorrect_ocr"] = summary["total_records"] - summary["correct_ocr"]
    summary["tfidf_predicted_wrong"] = summary["tfidf_predicted"] - summary["tfidf_predicted_correct"]
    summary["accuracy (%)"] = (summary["correct_ocr"] / summary["total_records"] * 100).round(2)
    summary["error_rate (%)"] = (summary["incorrect_ocr"] / summary["total_records"] * 100).round(2)
    summary["TFIDF_Predicted (%)"] = (summary["tfidf_predicted"] / summary["total_records"] * 100).round(2)
    summary["TFIDF_Predict_Accuracy (%)"] = (
        summary["tfidf_predicted_correct"] / summary["tfidf_predicted"].replace(0, np.nan) * 100
    ).fillna(0).round(2)
    summary["TFIDF_Predict_Error (%)"] = (
        summary["tfidf_predicted_wrong"] / summary["tfidf_predicted"].replace(0, np.nan) * 100
    ).fillna(0).round(2)
    summary["predict_navative_rate (%)"] = (summary["predict_nagative"] / summary["total_records"] * 100).round(2)
    summary["TFIDF_Overall_Accuracy (%)"] = (summary["tfidf_final_correct"] / summary["total_records"] * 100).round(2)
    summary["min_time_ms"] = summary["min_time_ms"].round(2)
    summary["max_time_ms"] = summary["max_time_ms"].round(2)
    summary["avg_time_ms"] = summary["avg_time_ms"].round(2)
    summary["min_confidence"] = summary["min_confidence"].round(2)
    summary["max_confidence"] = summary["max_confidence"].round(2)  
    summary["avg_confidence"] = summary["avg_confidence"].round(2)
    summary = summary[
        [
            "doctypefieldcode", "total_records", "correct_ocr", "incorrect_ocr",
            "accuracy (%)", "error_rate (%)",
            "tfidf_predicted", "TFIDF_Predicted (%)",
            "tfidf_predicted_correct", "tfidf_predicted_wrong",
            "TFIDF_Predict_Accuracy (%)", "TFIDF_Predict_Error (%)",
            "TFIDF_Overall_Accuracy (%)",
            "predict_nagative", "predict_navative_rate (%)",
            "min_time_ms", "max_time_ms", "avg_time_ms",
            "min_confidence", "max_confidence", "avg_confidence"
        ]
    ].sort_values(by="error_rate (%)", ascending=False)

    os.makedirs("result", exist_ok=True)
    df.to_csv("result/tfidf_prediction_full_output.csv", index=False)
    summary.to_csv("result/tfidf_model_summary.csv", index=False)
    print(summary.to_string(index=False))

if __name__ == "__main__":
    run_replay(CSV_PATH)