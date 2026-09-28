# Daily_Task

## 改完程式要跑的測試

`Email_Check/test/` 底下五個檔，直接跑檔案本身，不需要 pytest，全部跑完約 36 秒。
改了 `statemachine.py`、`task_list/task_list_gui.py` 或 `calendar_check.py` 就跑對應的那一個。

```
conda run -n ML --no-capture-output python "D:\dont_move\git_save\Daily_Task\Email_Check\test\test_statemachine.py"
```

`--no-capture-output` 不能省。測試會印中文，conda 把子行程的輸出接回來重印時走 cp950，
會丟 UnicodeEncodeError，看起來像測試爆掉，其實測試本身是通過的。

## 待辦清單上的「收件」與「收信」兩欄

值不是這個 repo 算出來的。`收件` 是這封信在被轉進來之前原本寄到哪個信箱，
`收信` 是那個信箱收到它的時間，兩個都由 Gmail MCP 算好之後，由排程的 LLM 照抄進
`round.json`。推導規則在 `D:\dont_move\git_save\gmail_mcp\server.py` 的
`_origin_mailbox` 與 `_received`，那個目錄不在版控裡，改壞了沒有歷史可以回。

改那邊的判斷規則要跑 `gmail_mcp/test_origin_mailbox.py`，它蓋住了兩個實際誤判過的
標頭形狀，包含群發信整份走 Bcc、連 `To` 都沒有的那一種。

## 不要為了省 token 去動 INSTRUCTIONS.md

2026-09-20 量過並否決。四個 Gmail 任務在 Claude Code `/usage` 的 24 小時用量歸因裡排不上一行，
而三個看似只是給人看的段落逐段查證後都帶著執行時規則，**很少用到不等於執行時用不到**。
壓縮流程與減少讀取信件內文也一併否決，前者動到使用者確認過的判例，
後者違反「漏掉一封該辦的信比多發一則通知糟得多」，而且這個任務失敗時是無聲的。

要重開這件事，先拿出新的歸因資料顯示排程執行確實佔了可觀比例，或是一次可以追溯到
指令檔組織方式的執行失敗。兩者都沒有就不要再量一次。

那次量測時一天只跑四輪。2026-09-28 起改成兩個排程任務，每小時 50 分與 20 分各一個時段，45 分鐘內剛跑完一輪的時段會在模型啟動前被 hook 擋掉，所以實際大約每小時跑完一輪，白天約十六輪，那份歸因已經不代表現在的用量。這本身不構成重開的理由，要重開仍然要先拿出新的歸因資料。
