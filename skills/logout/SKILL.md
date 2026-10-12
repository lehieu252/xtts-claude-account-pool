---
name: logout
description: Xoá một account Claude khỏi danh sách đã lưu.
argument-hint: "<tên | email | phần trước @>"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" remove $ARGUMENTS 2>&1 || true`

Hiển thị NGUYÊN VĂN kết quả ở trên cho người dùng (giữ nguyên bảng markdown), không thêm nhận xét, không chạy thêm lệnh nào.
