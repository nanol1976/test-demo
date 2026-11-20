"""后台把子进程的输出读走。

Windows 的匿名管道缓冲区只有几 KB，而我们起的服务每处理一次写操作就打一行 INFO 日志。
验收脚本如果不读 stdout，服务写满缓冲区后就会堵在 write() 上，客户端表现成「请求超时」。
Linux 管道缓冲区大得多，所以同一份脚本在云主机上一直是绿的。

用法：
    from _proc_drain import drain
    lines = drain(server)        # 返回一个持续增长的列表
    ...
    print("".join(lines[-40:]))  # 失败时打日志尾部
"""
import threading


def drain(proc, keep=400):
    """起一个守护线程把 proc.stdout 读完，返回「最近 keep 行」的列表。"""
    lines = []

    def _loop():
        try:
            for line in proc.stdout:
                lines.append(line)
                if len(lines) > keep:
                    del lines[0]
        except Exception:
            pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return lines
