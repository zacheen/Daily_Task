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
