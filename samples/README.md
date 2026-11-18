# 样例数据

`day1.csv` / `day2.csv`：2024-05-01 与 2024-05-02 两个连续 UTC 日的闸机记录。
`correction.csv`：对 `E2`（跨午夜出站）的修订号 1 更正，可用来观察两日同时重算。

清单核收样例（表头 `事件号,修订号`）：

* `manifest_west.csv`：西直门 2024-05-01 的完整清单（E1/E3），
  与 `day1.csv` 受理后应判为**核齐**。
* `manifest_west_incomplete.csv`：多列了 `E7`，应出现 1 条**缺失**（修订不符 / 清单外均为 0）。

在页面「上传站点日完整清单」里站点填 `西直门`、日期填 `2024-05-01` 即可复现；
再次上传任一清单会替换旧清单并立即撤回旧结论。

无 Docker 时本地运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000
```
