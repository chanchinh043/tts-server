#!/usr/bin/env python3
# step1_generate_kokoro_audio.py
#
# Bước 1/9 của pipeline "remote TTS pack": generate audio thô (.wav) cho
# TOÀN BỘ từ/câu/cụm của 1 hoặc nhiều bài đọc, bằng ĐÚNG model Kokoro đang
# dùng trên Android (sherpa-onnx, kokoro-multi-lang-v1_0) — để audio sinh ra
# ở đây và audio tự sinh trên máy (KokoroTtsEngine) là CÙNG MỘT MODEL, tránh
# lệch giọng/chất lượng giữa 2 nguồn.
#
# ⚠️ QUAN TRỌNG VỀ --model-dir:
# Dựa theo cây thư mục bạn cung cấp (kokoro-sherpa-full\...), bộ file model
# thật sự nằm trong thư mục con "kokoro", không phải thư mục gốc
# "kokoro-sherpa-full". Tức là --model-dir phải trỏ vào:
#     .../kokoro-sherpa-full/kokoro
# (thư mục này chứa model.onnx, voices.bin, tokens.txt, espeak-ng-data/,
#  lexicon-us-en.txt, lexicon-gb-en.txt, lexicon-zh.txt, dict/, và các file
#  rule_fsts date-zh.fst / number-zh.fst / phone-zh.fst).
# Đây CHÍNH LÀ bản kokoro-multi-lang-v1_0 đầy đủ (có hỗ trợ tiếng Trung),
# nên ngoài model/voices/tokens/lexicon/data_dir như bản cũ, script này còn
# tự động gắn thêm dict_dir (jieba, cho phân đoạn từ tiếng Trung) và
# rule_fsts (chuẩn hoá ngày/số/số điện thoại tiếng Trung) nếu các file đó
# tồn tại trong --model-dir. Với nội dung tiếng Anh (text_en) thì phần
# tiếng Trung này không ảnh hưởng gì, chỉ là gắn đúng cấu hình gốc của gói.
#
# ── VỀ HASH/TÊN FILE ─────────────────────────────────────────────────────
# Script này CHƯA đặt tên file theo đúng quy ước cache của TtsAudioCache
# (dạng "{type}_{itemId}_{contentHash}.ext") — vì chưa biết chính xác công
# thức hash đang dùng bên Kotlin (TtsAudioCache.contentHash chưa được xem).
# Tạm thời lưu theo tên item_id thuần (dễ đọc, dễ debug), việc đổi tên đúng
# chuẩn cache sẽ làm ở bước đóng gói (sau khi xác nhận lại TtsAudioCache.kt).
#
# ── CẤU TRÚC OUTPUT ──────────────────────────────────────────────────────
# {output_dir}/{reading_id}/{sid}/{type}_{item_id}.wav
#   type ∈ {"word", "sentence", "phrase"} — khớp ý nghĩa TtsCacheItemType
#   bên Kotlin (tên .prefix cụ thể sẽ đối chiếu lại ở bước đóng gói).
#
# ── CÀI ĐẶT CẦN THIẾT ────────────────────────────────────────────────────
#   pip install sherpa-onnx numpy
#
# ── CÁCH DÙNG (không cần dòng lệnh, không cần launch.json) ───────────────
# Sửa trực tiếp các biến trong khối "CẤU HÌNH" ngay bên dưới docstring này
# (MODEL_DIR, READINGS_DB, MYREADING_DB, OUTPUT_DIR, SIDS, READING_IDS,
# SPEED, NUM_THREADS), rồi bấm Run/F5 — kể cả chạy qua debugpy như log bạn
# gửi (nơi các --arg dòng lệnh thường KHÔNG được chuyển vào script nếu
# không khai báo "args" trong launch.json) — script vẫn chạy đúng vì các
# giá trị đã nằm sẵn trong file .py, không phụ thuộc đối số dòng lệnh.
#
# Nếu OUTPUT_DIR / READINGS_DB / MYREADING_DB để trống (""), script tự
# dùng thư mục cạnh file .py này:
#   step1_generate_kokoro_audio.py
#   db/readings.db, db/myreading.db   (nếu để trống 2 biến DB)
#   generated_audio/                  (nếu để trống OUTPUT_DIR)
#
# Vẫn có thể truyền cờ dòng lệnh (--model-dir, --sids, ...) như cách cũ
# nếu muốn — cờ dòng lệnh sẽ ưu tiên hơn giá trị trong khối CẤU HÌNH.
package_doc = __doc__  # giữ lại docstring ở trên để --help hiển thị đẹp

# ══════════════════════════════════════════════════════════════════════
# CẤU HÌNH — SỬA TRỰC TIẾP CÁC DÒNG DƯỚI ĐÂY.
# Không cần truyền tham số dòng lệnh, không cần cấu hình launch.json.
# Bấm Run/F5 (kể cả qua debugpy) là chạy được ngay với giá trị bên dưới.
# Các giá trị này chỉ là MẶC ĐỊNH — nếu bạn vẫn truyền cờ dòng lệnh
# (--model-dir, --sids, ...) thì cờ dòng lệnh sẽ được ưu tiên hơn.
# ══════════════════════════════════════════════════════════════════════

# Thư mục chứa model Kokoro — PHẢI trỏ vào thư mục con "kokoro" (chứa
# model.onnx, voices.bin, tokens.txt, espeak-ng-data/, ...), KHÔNG phải
# thư mục gốc "kokoro-sherpa-full".
MODEL_DIR = r"D:\Website\App Androi\TTS\Download model\kokoro-sherpa-full\kokoro"

# Đường dẫn 2 file DB. Để chuỗi rỗng "" nếu không có / không dùng loại đó.
READINGS_DB = r""    # vd: r"D:\Website\App Androi\TTS\...\readings.db"
MYREADING_DB = r""   # vd: r"D:\Website\App Androi\TTS\...\myreading.db"

# Thư mục ghi WAV output. Để chuỗi rỗng "" để dùng thư mục
# "generated_audio" nằm cạnh file .py này.
OUTPUT_DIR = r""

# Danh sách sid cần generate.
SIDS = [2, 3, 9, 11, 16, 26]

# Chỉ generate các reading_id này. Để [] = TOÀN BỘ bài trong DB.
READING_IDS: list = []

# Tốc độ đọc — TÁCH RIÊNG theo item_type, vì word (đọc rời từng từ) cần
# giữ tốc độ chuẩn để phát âm rõ, còn sentence/phrase (đọc liền mạch) đọc
# chậm hơn cho dễ nghe. Đặt Ở ĐÂY (trong pipeline generate), KHÔNG phải ở
# server (job_processor.py) hay batch script gọi nó — vì CẢ 2 nơi đó đều
# gọi chung generate_with_fallback() bên dưới, đặt tốc độ ở tầng này đảm
# bảo audio bài hệ thống (batch, upload Drive thủ công) và audio MyReading
# (server, tự động) LUÔN cùng 1 tốc độ cho cùng 1 loại item, không thể bị
# lệch do quên đồng bộ ở 2 nơi gọi khác nhau.
SPEED_WORD = 1.0
SPEED_SENTENCE = 0.90
SPEED_PHRASE = 0.90
NUM_THREADS = 2
# ══════════════════════════════════════════════════════════════════════

import argparse
import sqlite3
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

# ── Thư mục gốc = nơi đặt file .py này (không phụ thuộc đang chạy từ đâu).
# Dùng làm fallback cho OUTPUT_DIR/DB khi để trống ở khối CẤU HÌNH trên.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_DIR = SCRIPT_DIR / "db"
DEFAULT_READINGS_DB = Path(READINGS_DB) if READINGS_DB else DEFAULT_DB_DIR / "readings.db"
DEFAULT_MYREADING_DB = Path(MYREADING_DB) if MYREADING_DB else DEFAULT_DB_DIR / "myreading.db"
DEFAULT_OUTPUT_DIR = Path(OUTPUT_DIR) if OUTPUT_DIR else SCRIPT_DIR / "generated_audio"
DEFAULT_MODEL_DIR = Path(MODEL_DIR) if MODEL_DIR else SCRIPT_DIR / "kokoro"
DEFAULT_SIDS = SIDS
DEFAULT_READING_IDS = READING_IDS or None
DEFAULT_NUM_THREADS = NUM_THREADS

try:
    import sherpa_onnx
except ImportError:
    print(
        "Thiếu thư viện sherpa-onnx. Cài bằng: pip install sherpa-onnx",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Ngưỡng câm + số lần thử lại — CÙNG giá trị với
# TtsCacheAuditor.SILENCE_AMPLITUDE_THRESHOLD / MAX_ATTEMPTS_PER_VOICE bên
# Kotlin, để hành vi phát hiện câm nhất quán giữa 2 phía. ──────────────────
SILENCE_AMPLITUDE_THRESHOLD = 0.01
MAX_ATTEMPTS_PER_VOICE = 3

# ── Các nhóm giọng thay thế cho nhau khi bị câm — CÙNG nội dung với
# TtsVoicePairing.kt (GROUPS). Nếu sau này đổi nhóm bên Kotlin, phải sửa
# lại đúng ở đây để 2 bên khớp nhau. ────────────────────────────────────────
VOICE_GROUPS: List[List[int]] = [
    [2, 3, 9],     # nhóm nữ: af_bella, af_heart, af_sarah
    [11, 26, 16],  # nhóm nam: am_adam, bm_george, am_michael
]

_FALLBACK_CHAIN: Dict[int, List[int]] = {}
for _group in VOICE_GROUPS:
    for _sid in _group:
        _FALLBACK_CHAIN[_sid] = [s for s in _group if s != _sid]


def fallback_chain_of(sid: int) -> List[int]:
    return _FALLBACK_CHAIN.get(sid, [])


# ── Tra cứu tốc độ theo item_type — DUY NHẤT nơi quyết định "loại item nào
# đọc tốc độ bao nhiêu" trong toàn hệ thống. generate_with_fallback() bên
# dưới gọi hàm này thay vì nhận speed từ caller — server (job_processor.py)
# và batch script bài hệ thống chỉ cần truyền item_type, KHÔNG tự quyết
# định/tính speed ở tầng của mình nữa, tránh lệch tốc độ giữa 2 nguồn audio.
_SPEED_BY_TYPE: Dict[str, float] = {
    "word": SPEED_WORD,
    "sentence": SPEED_SENTENCE,
    "phrase": SPEED_PHRASE,
}
_DEFAULT_SPEED_FOR_UNKNOWN_TYPE = SPEED_SENTENCE


def get_speed_for_type(item_type: str) -> float:
    speed = _SPEED_BY_TYPE.get(item_type)
    if speed is None:
        print(
            f"    ⚠ get_speed_for_type: item_type={item_type!r} không thuộc "
            f"{list(_SPEED_BY_TYPE.keys())}, dùng mặc định {_DEFAULT_SPEED_FOR_UNKNOWN_TYPE}"
        )
        return _DEFAULT_SPEED_FOR_UNKNOWN_TYPE
    return speed


# ── Data model đọc từ DB — khớp ý nghĩa TtsWordItem/TtsSentenceItem/
# TtsPhraseItem bên TtsReadingContentReader.kt. ────────────────────────────
@dataclass
class Item:
    item_type: str  # "word" | "sentence" | "phrase"
    item_id: str
    sentence_id: Optional[str]
    text_en: str


def open_readonly(db_path: Path) -> sqlite3.Connection:
    # mode=ro giống tinh thần OPEN_READONLY bên Kotlin — tránh khoá/ghi nhầm
    # nếu app đang chạy song song truy cập cùng file (hiếm khi xảy ra ở máy
    # dev, nhưng cứ theo đúng nguyên tắc "chỉ đọc" của pipeline này).
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def get_reading_ids(db_path: Path, has_deleted_at: bool) -> List[str]:
    with open_readonly(db_path) as conn:
        query = "SELECT reading_id FROM readings"
        if has_deleted_at:
            query += " WHERE deleted_at IS NULL"
        return [row[0] for row in conn.execute(query)]


def get_items_for_reading(db_path: Path, reading_id: str) -> List[Item]:
    items: List[Item] = []
    with open_readonly(db_path) as conn:
        cur = conn.execute(
            "SELECT sentence_id, text_en FROM reading_sentences WHERE reading_id = ?",
            (reading_id,),
        )
        for sentence_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("sentence", sentence_id, sentence_id, text_en))

        cur = conn.execute(
            """
            SELECT w.word_id, w.sentence_id, w.text_en
            FROM sentence_words w
            INNER JOIN reading_sentences s ON s.sentence_id = w.sentence_id
            WHERE s.reading_id = ?
            """,
            (reading_id,),
        )
        for word_id, sentence_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("word", word_id, sentence_id, text_en))

        cur = conn.execute(
            """
            SELECT p.phrase_id, p.sentence_id, p.text_en
            FROM sentence_phrases p
            INNER JOIN reading_sentences s ON s.sentence_id = p.sentence_id
            WHERE s.reading_id = ?
            """,
            (reading_id,),
        )
        for phrase_id, sentence_id, text_en in cur:
            if text_en and text_en.strip():
                items.append(Item("phrase", phrase_id, sentence_id, text_en))

    return items


def build_tts(model_dir: Path, num_threads: int) -> "sherpa_onnx.OfflineTts":
    # ⚠️ Tên file bên dưới theo đúng chuẩn gói kokoro-multi-lang-v1_0 chính
    # thức của sherpa-onnx, và đúng khớp với cây thư mục thực tế:
    #     kokoro-sherpa-full/kokoro/model.onnx
    #     kokoro-sherpa-full/kokoro/voices.bin
    #     kokoro-sherpa-full/kokoro/tokens.txt
    #     kokoro-sherpa-full/kokoro/espeak-ng-data/
    #     kokoro-sherpa-full/kokoro/lexicon-us-en.txt
    #     kokoro-sherpa-full/kokoro/lexicon-gb-en.txt
    #     kokoro-sherpa-full/kokoro/lexicon-zh.txt
    #     kokoro-sherpa-full/kokoro/dict/            (jieba dict, tiếng Trung)
    #     kokoro-sherpa-full/kokoro/date-zh.fst
    #     kokoro-sherpa-full/kokoro/number-zh.fst
    #     kokoro-sherpa-full/kokoro/phone-zh.fst
    # Nếu bộ model trong assets/kokoro/ của app Android đặt tên khác hoặc
    # thiếu file nào, SỬA LẠI đúng tên tương ứng ở đây trước khi chạy —
    # bắt buộc phải khớp 100% với bộ đang copy ra bằng AssetCopier.kt để
    # đảm bảo cùng 1 model (sid trong voices.bin phải cùng thứ tự).
    model_path = model_dir / "model.onnx"
    voices_path = model_dir / "voices.bin"
    tokens_path = model_dir / "tokens.txt"
    data_dir = model_dir / "espeak-ng-data"
    dict_dir = model_dir / "dict"

    lexicon_candidates = [
        model_dir / "lexicon-us-en.txt",
        model_dir / "lexicon-gb-en.txt",
        model_dir / "lexicon-zh.txt",
    ]
    lexicon_path = ",".join(str(p) for p in lexicon_candidates if p.exists())

    rule_fst_candidates = [
        model_dir / "date-zh.fst",
        model_dir / "number-zh.fst",
        model_dir / "phone-zh.fst",
    ]
    rule_fsts_path = ",".join(str(p) for p in rule_fst_candidates if p.exists())

    for required in (model_path, voices_path, tokens_path):
        if not required.exists():
            print(f"LỖI: không tìm thấy file model bắt buộc: {required}", file=sys.stderr)
            print(
                "  → Kiểm tra lại --model-dir: theo cây thư mục bạn gửi, nó phải "
                "trỏ vào thư mục con \"kokoro\" (vd: .../kokoro-sherpa-full/kokoro), "
                "không phải thư mục gốc \"kokoro-sherpa-full\".",
                file=sys.stderr,
            )
            sys.exit(1)

    kokoro_kwargs = dict(
        model=str(model_path),
        voices=str(voices_path),
        tokens=str(tokens_path),
        data_dir=str(data_dir) if data_dir.exists() else "",
        lexicon=lexicon_path,
    )

    # dict_dir / rule_fsts chỉ tồn tại trên bản sherpa-onnx hỗ trợ đúng
    # kokoro-multi-lang (có Chinese). Thử gắn thêm, nếu binding không có
    # field này thì rơi về cấu hình cũ (vẫn chạy tốt cho tiếng Anh).
    optional_kwargs = {}
    if dict_dir.exists():
        optional_kwargs["dict_dir"] = str(dict_dir)
    if rule_fsts_path:
        optional_kwargs["rule_fsts"] = rule_fsts_path

    kokoro_config = None
    if optional_kwargs:
        try:
            kokoro_config = sherpa_onnx.OfflineTtsKokoroModelConfig(
                **kokoro_kwargs, **optional_kwargs
            )
        except TypeError as e:
            print(
                f"  (Bỏ qua dict_dir/rule_fsts — bản sherpa-onnx đang cài "
                f"không hỗ trợ field này: {e})",
                file=sys.stderr,
            )
            kokoro_config = None

    if kokoro_config is None:
        kokoro_config = sherpa_onnx.OfflineTtsKokoroModelConfig(**kokoro_kwargs)

    model_config = sherpa_onnx.OfflineTtsModelConfig(
        kokoro=kokoro_config,
        num_threads=num_threads,
        provider="cpu",
    )
    tts_config = sherpa_onnx.OfflineTtsConfig(model=model_config, max_num_sentences=1)
    return sherpa_onnx.OfflineTts(tts_config)


def max_amplitude(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def save_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    # PCM16 mono, header 44 byte chuẩn — CÙNG định dạng TtsAudioCache đang
    # ghi bên Kotlin, để TtsCacheAuditor (nếu sau này audit lại thủ công)
    # đọc được y hệt cách đang đọc file tự sinh trên máy.
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())


def generate_with_fallback(
    tts: "sherpa_onnx.OfflineTts",
    text: str,
    primary_sid: int,
    item_type: str,
) -> Optional[tuple]:
    # ── Trả về (samples, sample_rate, sid_dùng_để_generate) nếu thành công,
    # None nếu HẾT TOÀN BỘ chuỗi giọng mà vẫn câm — cùng tinh thần
    # repairSilentItem() bên TtsPregenWorker.kt (thử giọng gốc trước, hết
    # MAX_ATTEMPTS_PER_VOICE lần vẫn câm thì sang giọng kế tiếp cùng nhóm). ─
    #
    # ⚠️ speed KHÔNG còn là tham số nhận từ caller — tự tra theo item_type
    # qua get_speed_for_type() ở trên, để CẢ server lẫn batch script gọi
    # hàm này đều dùng chung đúng 1 chính sách tốc độ, không thể lệch nhau.
    speed = get_speed_for_type(item_type)
    sid_chain = [primary_sid] + fallback_chain_of(primary_sid)

    for sid in sid_chain:
        for attempt in range(1, MAX_ATTEMPTS_PER_VOICE + 1):
            audio = tts.generate(text, sid=sid, speed=speed)
            samples = np.array(audio.samples, dtype=np.float32)
            amp = max_amplitude(samples)
            if amp >= SILENCE_AMPLITUDE_THRESHOLD:
                return samples, audio.sample_rate, sid
            print(
                f"    câm: sid={sid} lần {attempt}/{MAX_ATTEMPTS_PER_VOICE} "
                f"(biên độ={amp:.4f}) text=\"{text}\""
            )

    return None


def run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    model_dir = Path(args.model_dir)

    print(f"Đang load model Kokoro từ {model_dir} ...")
    tts = build_tts(model_dir, args.num_threads)
    print("Model sẵn sàng.")

    # ── Tự dò DB trong thư mục db/ cạnh file .py nếu người dùng không
    # truyền --readings-db / --myreading-db. Chỉ đưa vào danh sách xử lý
    # nếu file thực sự tồn tại, để tránh lỗi mở file rỗng khi chỉ có 1
    # trong 2 loại DB. ──────────────────────────────────────────────────
    readings_db_path = Path(args.readings_db) if args.readings_db else DEFAULT_READINGS_DB
    myreading_db_path = Path(args.myreading_db) if args.myreading_db else DEFAULT_MYREADING_DB

    reading_sources: List[tuple] = []
    if readings_db_path.exists():
        reading_sources.append((readings_db_path, False))
    elif args.readings_db:
        # Người dùng chỉ định rõ đường dẫn nhưng không tồn tại -> báo lỗi cứng.
        print(f"LỖI: không tìm thấy --readings-db: {readings_db_path}", file=sys.stderr)
        sys.exit(1)

    if myreading_db_path.exists():
        reading_sources.append((myreading_db_path, True))
    elif args.myreading_db:
        print(f"LỖI: không tìm thấy --myreading-db: {myreading_db_path}", file=sys.stderr)
        sys.exit(1)

    if not reading_sources:
        print(
            f"Không tìm thấy DB nào. Đã kiểm tra mặc định:\n"
            f"  {DEFAULT_READINGS_DB}\n"
            f"  {DEFAULT_MYREADING_DB}\n"
            f"Hãy đặt file DB vào thư mục '{DEFAULT_DB_DIR}' (cạnh file .py), "
            f"hoặc truyền --readings-db / --myreading-db thủ công.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Nguồn DB sẽ dùng:")
    for db_path, _ in reading_sources:
        print(f"  - {db_path}")

    all_reading_ids: List[tuple] = []
    for db_path, has_deleted_at in reading_sources:
        ids = get_reading_ids(db_path, has_deleted_at)
        all_reading_ids.extend((rid, db_path) for rid in ids)

    if args.reading_ids:
        wanted = set(args.reading_ids)
        all_reading_ids = [(rid, db) for rid, db in all_reading_ids if rid in wanted]

    print(f"Tổng số bài sẽ generate: {len(all_reading_ids)}")

    total_items = 0
    total_silent_failed = 0
    started_at = time.time()

    for reading_id, db_path in all_reading_ids:
        items = get_items_for_reading(db_path, reading_id)
        print(f"\n[{reading_id}] {len(items)} item (word+sentence+phrase)")

        for sid in args.sids:
            for item in items:
                out_path = output_dir / reading_id / str(sid) / f"{item.item_type}_{item.item_id}.wav"
                if out_path.exists():
                    # Resume tự nhiên — item nào đã sinh rồi thì bỏ qua,
                    # cùng tinh thần processItem() bên Kotlin (đã có cache
                    # thì không generate lại).
                    continue

                result = generate_with_fallback(tts, item.text_en, sid, item.item_type)
                total_items += 1

                if result is None:
                    total_silent_failed += 1
                    print(
                        f"  [{item.item_type}] id={item.item_id} sid={sid}: "
                        f"HẾT CHUỖI GIỌNG VẪN CÂM, text=\"{item.text_en}\" — bỏ qua"
                    )
                    continue

                samples, sample_rate, used_sid = result
                save_wav(out_path, samples, sample_rate)
                if used_sid != sid:
                    print(
                        f"  [{item.item_type}] id={item.item_id} sid={sid}: "
                        f"sinh bằng giọng thay thế sid={used_sid} (giọng gốc câm)"
                    )

    elapsed = time.time() - started_at
    print(
        f"\nXong. Tổng {total_items} item đã xử lý, "
        f"{total_silent_failed} item câm hoàn toàn (bỏ qua), "
        f"mất {elapsed:.1f}s."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate audio Kokoro cho từ/câu/cụm của bài đọc (bước 1/9).",
    )
    parser.add_argument(
        "--readings-db",
        default=None,
        help=(
            "Đường dẫn readings.db (bài hệ thống). "
            f"Mặc định: '{DEFAULT_READINGS_DB}' (thư mục 'db' cạnh file .py này)."
        ),
    )
    parser.add_argument(
        "--myreading-db",
        default=None,
        help=(
            "Đường dẫn myreading.db (bài user tạo). "
            f"Mặc định: '{DEFAULT_MYREADING_DB}' (thư mục 'db' cạnh file .py này)."
        ),
    )
    parser.add_argument(
        "--model-dir",
        default=str(DEFAULT_MODEL_DIR),
        help=(
            "Thư mục chứa model Kokoro (giống assets/kokoro). "
            "Theo cây thư mục hiện tại của bạn, đây là "
            "'.../kokoro-sherpa-full/kokoro' — KHÔNG phải thư mục gốc "
            f"'kokoro-sherpa-full'. Mặc định (sửa ở biến MODEL_DIR đầu file): "
            f"'{DEFAULT_MODEL_DIR}'."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Thư mục lưu WAV output. "
            f"Mặc định (sửa ở biến OUTPUT_DIR đầu file): '{DEFAULT_OUTPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--sids",
        type=int,
        nargs="+",
        default=DEFAULT_SIDS,
        help=(
            "Danh sách sid cần generate, vd: 2 3 9. "
            f"Mặc định (sửa ở biến SIDS đầu file): {DEFAULT_SIDS}"
        ),
    )
    parser.add_argument(
        "--reading-ids",
        nargs="*",
        default=DEFAULT_READING_IDS,
        help=(
            "Chỉ generate các bài này (bỏ trống = tất cả). "
            f"Mặc định (sửa ở biến READING_IDS đầu file): {DEFAULT_READING_IDS}"
        ),
    )
    # ⚠️ Đã BỎ --speed — tốc độ giờ tự động theo item_type (word/sentence/
    # phrase), xem SPEED_WORD/SPEED_SENTENCE/SPEED_PHRASE ở khối CẤU HÌNH
    # đầu file. Muốn đổi tốc độ, sửa trực tiếp 3 hằng số đó, không truyền
    # cờ dòng lệnh nữa.
    parser.add_argument(
        "--num-threads",
        type=int,
        default=DEFAULT_NUM_THREADS,
        help=f"Số luồng CPU cho model. Mặc định (biến NUM_THREADS đầu file): {DEFAULT_NUM_THREADS}",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())