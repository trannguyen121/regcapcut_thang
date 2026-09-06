# Reg CapCut

Snapshot mã nguồn hiện tại: Reg, Reg treo, Add Link, Check Pro và lấy user.
Các luồng CapCut dùng Chrome for Testing 152 ẩn danh, tách dữ liệu từng tài khoản.
Add Link lưu lịch sử theo email và kiểm tra tư cách thành viên trước/sau Submit.

## Chạy từ source

1. Cài Python 3.12 và chạy `python -m pip install -r requirements.txt`.
2. Sao chép `core/settings.example.json` thành `core/settings.json`, rồi điền cấu hình riêng.
3. Đặt Chrome for Testing 152 vào `chrome-152/chrome.exe`, hoặc chọn đường dẫn trong Settings.
4. Chạy `run_reg_capcut.bat` (hoặc `run_reg_capcut_v8.bat` nếu dùng launcher v8).

Các file tài khoản, link, proxy, cookie, cấu hình thật, log, kết quả và browser
không được commit. Nhập những dữ liệu này trên máy sử dụng ứng dụng.
Các module dùng chung với tool cũ được giữ để hỗ trợ giao diện tổng hợp và test;
phần PaySafe chứa thông tin đăng nhập riêng không nằm trong repository này.

## Kiểm thử

```powershell
python -m unittest discover -s tests
```

Kiểm thử trình duyệt cần Chrome for Testing 152 trên máy; test dùng dữ liệu giả lập,
không đăng nhập tài khoản CapCut thật.

## Build

Để tạo bản phát hành mới, chỉ sửa số trong `BUILD_VERSION.txt` (ví dụ `8.1`
thành `8.2`), sau đó chạy `build_regcapcut_release.bat`. Tên hiển thị trong GUI,
tên EXE và thư mục trong `dist` sẽ tự dùng cùng số phiên bản.

Các file `.spec` hiện tại cần `core/settings.json` và `cred.json` trên máy.
Có thể sao chép `cred.example.json` thành `cred.json` để build; muốn ghi Google
Sheets thì phải cấu hình thông tin service account thật trong file riêng đó.

```powershell
python -m PyInstaller --noconfirm --clean --distpath build/package --workpath build/work regcapcut_v7.9.spec
```

Build vào thư mục riêng để giữ nguyên bản phát hành, dữ liệu và Chromium đang có.
File build release tự sao chép `dist/chrome-152` vào cạnh EXE để bản phát hành
luôn chạy đúng Chrome for Testing 152.
