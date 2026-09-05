# Codex 宠物包安装与删除设计

## 目标

让桌宠设置面板直接管理当前 Windows 用户的 Codex 宠物目录 `%USERPROFILE%\.codex\pets`，并与以下官方安装方式保持一致：

```text
npx codex-pet-installer add kitagawa-marin
```

用户可以输入宠物 id，也可以粘贴完整的上述 `npx` 命令。已安装的 Codex 宠物可在界面中选择和删除。

## 输入与安全边界

安装输入仅接受两种格式：

- 单独的宠物 id，例如 `kitagawa-marin`。
- 固定命令格式 `npx codex-pet-installer add <pet-id>`，允许 `npx --yes codex-pet-installer add <pet-id>`。

服务端只从输入中解析出符合 `[a-z0-9-]+` 的宠物 id，不把用户输入直接交给 shell。实际执行使用固定参数数组调用 `npx.cmd --yes codex-pet-installer add <pet-id>`，避免命令注入。

## 服务端

宠物根目录改为 `CODEX_HOME/pets`；未配置 `CODEX_HOME` 时使用当前用户主目录下的 `.codex/pets`。列表接口只返回同时具有有效 `pet.json` 与精灵图文件的完整宠物包，并兼容清单中 `spritesheetPath`、`assets.spritesheet` 以及默认文件名。

`POST /v1/pets/install` 接收 JSON 或查询参数中的安装输入，解析宠物 id 后运行官方安装器。安装完成后校验目录、清单和精灵图；任何失败都返回非 2xx 状态与可读错误，不把残缺目录暴露到宠物列表。

新增 `DELETE /v1/pets/<id>`。服务端规范化并校验 id，解析目标绝对路径，确认它是宠物根目录的直接子目录后再删除。不存在时返回 404；成功时返回删除的宠物 id。

静态宠物文件接口从实际清单解析精灵图相对路径，但只允许读取宠物目录内部的 `.webp` 或 `.png` 文件。

## 前端

安装区域保留单个输入框，提示可填 id 或完整 `npx` 命令。安装按钮在请求期间禁用，成功后刷新列表、自动选择新宠物并切换到桌宠模式；失败时显示服务端错误。

每个已安装 Codex 宠物在选择项旁显示“删除”按钮。点击后请求删除接口；若删除的是当前宠物，立即回退到内置猫猫；随后刷新列表。内置宠物不提供删除按钮。

列表返回每只宠物的精灵图 URL，前端不再假定文件一定叫 `spritesheet.webp`。

## 错误处理

- Node.js、npm 或 `npx` 不可用：明确提示安装器无法启动。
- 安装器退出非零：返回经过长度限制的 stderr/stdout，便于定位网络或宠物 id 问题。
- 安装结果不完整：报告缺少清单或精灵图，不显示该宠物。
- 删除失败：保留界面状态并显示原因。
- 刷新列表失败：不清空当前已加载列表，避免瞬时服务错误造成界面跳变。

## 测试与验收

后端单元测试覆盖输入解析、Codex 目录选择、不同清单格式、目录穿越防护、安装器参数调用、安装结果校验和安全删除。前端静态/行为测试覆盖完整命令输入、安装请求、删除按钮、当前宠物删除后的回退行为。

验收样例：在输入框粘贴 `npx codex-pet-installer add kitagawa-marin` 后安装成功，`kitagawa-marin` 出现在列表并可显示；点击删除后对应目录从 `%USERPROFILE%\.codex\pets` 消失，列表同步移除，当前选择安全回退。
