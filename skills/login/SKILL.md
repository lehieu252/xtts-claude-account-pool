---
name: login
description: Đăng nhập account Claude bất kỳ (mới hoặc đăng nhập lại) và lưu vào danh sách — mở Terminal để hoàn tất đăng nhập, không đổi account đang dùng.
argument-hint: "<email | tên-account> [email]"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" login $ARGUMENTS 2>&1 || true`

Hiển thị NGUYÊN VĂN kết quả ở trên cho người dùng (giữ nguyên bảng markdown), không thêm nhận xét, không chạy thêm lệnh nào.
