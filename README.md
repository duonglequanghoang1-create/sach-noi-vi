# sach-noi-vi — Thư viện sách nói tiếng Việt

Pipeline tạo **sách nói tiếng Việt** chạy headless trên VPS, không cần API key
và không cần tài khoản ChatGPT:

```
nguồn (PDF/EPUB/txt)  →  trích xuất  →  dịch / chuẩn hoá tiếng Việt  →  TTS  →  M4B có mục lục + bìa
```

Giọng đọc: **VieNeu-TTS**, chạy offline trên máy.

## Vì sao repo này tồn tại

Hai công cụ có sẵn đều giải quyết đúng một nửa bài toán:

| | Sano | RetainPDF |
|---|---|---|
| Giọng đọc tiếng Việt, xuất M4B | ✅ | ❌ |
| Trích xuất + dịch PDF giữ bố cục | ❌ | ✅ |
| Chạy headless trên VPS | ❌ (app desktop) | ❌ (cần Docker) |

Cả hai đều là ứng dụng desktop, đều cần mở cửa sổ, đều không chạy được trên
một VPS trần. Repo này lấy phần *định nghĩa bài toán* từ Sano (chia chương them
Heading, chuẩn hoá lời đọc, M4B có mục lục chương + bìa) và phần *trích xuất
nội dung* từ RetainPDF, rồi viết lại thành script headless chạy được không cần
GUI.

## Bản quyền

**Chỉ những cuốn public domain hoặc giấy phép cho phép phát tán mới được đưa
vào đây.** Danh sách nằm ở `catalog/books.json`, mỗi cuốn có `license`,
`license_url` và `rights_note`.

Pipeline **từ chối** tạo audio cho bất kỳ cuốn nào mà giấy phép không thuộc:

```
public-domain · CC0 · CC-BY-4.0 · CC-BY-SA-4.0
```

Đây là lỗi cứng, không phải cảnh báo. Xem `CONTRACT.md` mục *Rights gate*.

Tự thêm sách của bạn: đặt file vào `books/<slug>/source/`, điền metadata vào
`book.json` với `license` hợp lệ, rồi chạy pipeline.

## Cài đặt

```bash
sudo apt-get install -y ffmpeg python3-pip python3-venv poppler-utils
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[extract,tts]'
cp .env.example .env
```

Model VieNeu-TTS tải một lần, xem `docs/tts.md`.

## Chạy

```bash
sach-noi extract  <slug>     # agent A — may1
sach-noi translate <slug>    # agent A — may1
sach-noi narrate  <slug>     # agent B — may2
sach-noi package  <slug>     # agent B — may2
sach-noi catalog             # agent B — may2
sach-noi all     <slug>      # chạy tuần tự tất cả
```

## Máy

| Agent | Máy | Phần |
|---|---|---|
| A | may1 | `extract/`, `translate/`, `scripts/` |
| B | may2 | `audio/`, `package.py`, `catalog.py`, `cli.py` |

Hai máy chạy độc lập, chỉ giao tiếp qua JSON trên git (`book.json`,
`library.json`). Chi tiết phân chia trong `CONTRACT.md`.

## Giấy phép

Code: MIT — xem [LICENSE](LICENSE). Sách: giấy phép riêng của từng cuốn.