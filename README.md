# claude-account-manager (`acct`)

Plugin Claude Code để quản lý nhiều account Claude (đăng nhập claude.ai): lưu account, xem usage 5 giờ / 7 ngày của tất cả account, và đổi account **không cần logout / login lại**. Dùng được trong CLI và extension VS Code, trên macOS, Windows và Linux.

| Lệnh | Việc làm |
|---|---|
| `/acct:login <email>` hoặc `/acct:login <tên> [email]` | Đăng nhập một account bất kỳ (mới hoặc đăng nhập lại) và lưu. Chỉ gõ email thì email là tên account, và script kiểm tra bạn đăng nhập đúng email đó trên trình duyệt. Mở cửa sổ Terminal để hoàn tất đăng nhập; **account đang dùng không bị đổi** |
| `/acct:save <tên>` | Lưu account **đang đăng nhập** vào danh sách, không cần đăng nhập lại |
| `/acct:list [--refresh]` | Bảng các account đã lưu: email, tổ chức, gói, usage 5h / 7 ngày, giờ reset. `▶` = account đang dùng |
| `/acct:switch <tên>` | Đổi sang account `<tên>`. Nhận tên, email hoặc phần trước `@` (vd `alice`) nếu không trùng account khác |
| `/acct:logout <tên>` | Xoá account `<tên>` khỏi danh sách (xoá token đã lưu) |

## Cài đặt

Trong Claude Code (CLI hoặc ô chat của extension VS Code):

```
/plugin marketplace add <github-owner>/claude-account-manager
/plugin install acct@claude-account-manager
```

Cần Python 3.8+ (`python3`, `python` hoặc `py -3`). Trên Windows, Claude Code chạy lệnh qua Git Bash.

## Dùng lần đầu

1. `/acct:save work`: lưu account đang dùng.
2. `/acct:login ban@example.com`: Terminal mở ra, trình duyệt mở trang đăng nhập. Đăng nhập đúng account, nếu trang hiện mã thì dán mã vào Terminal.
3. `/acct:list`: xem cả hai account và usage.

Từ đó trở đi chỉ cần `/acct:switch work` hoặc `/acct:switch ban`, rồi **mở tab Claude mới** và gõ `/resume` để tiếp tục cuộc hội thoại cũ. Tab đang mở có thể tự dùng account mới sau khoảng 30–60 giây vì Claude Code chỉ cache Keychain một lúc (theo README của claude-swap, Anthropic chưa xác nhận).

Token của account đã lưu hết hạn (lâu không dùng)? Gõ lại `/acct:login <tên>` để đăng nhập lại.

## Cách hoạt động

- Claude Code giữ đăng nhập ở **Keychain** (macOS, mục `Claude Code-credentials`) hoặc file `~/.claude/.credentials.json` (Windows / Linux). Email / tổ chức của account nằm ở `~/.claude.json`, khoá `oauthAccount`.
- `login` chạy `claude auth login` với `CLAUDE_CONFIG_DIR` trỏ vào thư mục tạm, nên đăng nhập đang dùng không bị đụng tới; xong thì chuyển token vào kho và xoá thư mục tạm. Trên macOS, đăng nhập đang dùng được sao lưu trước và tự khôi phục nếu Keychain không tách theo thư mục.
- `save` sao chép token đang dùng vào kho riêng: Keychain mục `claude-acct` (macOS) hoặc `~/.claude-acct/secrets/<tên>.json`, quyền 600 (Windows / Linux). Danh sách + usage cache ở `~/.claude-acct/accounts.json` (không chứa token).
- `switch` lưu lại token **mới nhất** của account hiện tại (Claude Code tự làm mới token theo thời gian) rồi ghi token + `oauthAccount` của account đích vào chỗ Claude Code đọc.
- `switch` từ chối nếu account đang đăng nhập chưa được lưu, để không làm mất đăng nhập đó (`--force` để bỏ qua).
- Token **không bao giờ** được in ra hội thoại; lệnh chạy script trực tiếp (`disable-model-invocation`), Claude chỉ hiển thị kết quả.

## Giới hạn

- **Usage dùng API không chính thức** (`api.anthropic.com/api/oauth/usage`, cùng nguồn với `/usage`). Anthropic có thể đổi bất cứ lúc nào. Kết quả được cache 2 phút để tránh lỗi 429.
- Account không dùng lâu sẽ có token hết hạn. Khi đó `list` hiện **số cũ kèm giờ cập nhật**; script không tự làm mới token để tránh làm hỏng phiên đăng nhập. Switch sang account đó một lần là có số mới.
- Đăng nhập claude.ai có thể yêu cầu dán mã, mà ô chat không nhận nhập liệu, nên `/acct:login` mở một cửa sổ terminal riêng (macOS: Terminal; Windows: cửa sổ Git Bash; Linux: terminal mặc định). Không mở được thì lệnh in ra câu lệnh để bạn tự chạy.
- Không hỗ trợ đăng nhập bằng API key, Bedrock, Vertex.
- Nếu bạn đặt `CLAUDE_CONFIG_DIR` trên macOS, tên mục Keychain có hậu tố theo đường dẫn; script tính theo quan sát cộng đồng, chưa có tài liệu chính thức.
- Dùng nhiều account subscription phải tuân theo điều khoản của Anthropic.

## Dùng từ terminal

```bash
bash scripts/acct.sh list
bash scripts/acct.sh switch work
```

Biến môi trường: `CLAUDE_ACCT_HOME` (thư mục kho, mặc định `~/.claude-acct`).
