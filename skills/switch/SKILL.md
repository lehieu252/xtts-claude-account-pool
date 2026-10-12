---
name: switch
description: Đổi sang account Claude khác đã lưu, không cần logout/login lại.
argument-hint: "<tên | email | phần trước @>"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" switch $ARGUMENTS 2>&1 || true`

Hiển thị NGUYÊN VĂN kết quả ở trên cho người dùng (giữ nguyên bảng markdown), không thêm nhận xét, không chạy thêm lệnh nào.
