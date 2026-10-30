"""确保目标库存在（容器/新环境首启必需，幂等）。

SeekDB 官方镜像的入口语义：**不带参数=常驻服务；带参数=执行该 SQL 后退出**，
因此无法用一次性容器去"给另一个实例建库"，库必须由平台侧建。
"""
import asyncio
import os
import sys

import pymysql


async def main() -> int:
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", "2881"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "")
    name = os.getenv("DB_NAME", "aiops")
    last = None
    for attempt in range(1, 31):          # 最多等 ~60s（数据库可能还在初始化）
        try:
            cn = pymysql.connect(host=host, port=port, user=user, password=password, connect_timeout=5)
            with cn.cursor() as c:
                c.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` DEFAULT CHARACTER SET utf8mb4")
            cn.close()
            print(f"[db_init] 库 {name} 就绪（第 {attempt} 次尝试）", flush=True)
            return 0
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[db_init] 等待数据库…（{attempt}/30）{type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(2)
    print(f"[db_init] 失败：{type(last).__name__}: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
