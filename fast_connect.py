# -*- coding: utf-8 -*-
#fast_connect.py
import os

PATH = os.path.join(os.path.dirname(__file__), "prompts")  # 実行場所に依存しない
ROLES = ("user", "system", "assistant", "goal", "judge",
         "system_pentest", "system_recon", "system_killchain",
         "reflect", "steps", "critic", "skill")


def load_prompt(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"存在しないrole: {role}")

    file_path = os.path.join(PATH, f"{role}.txt")

    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

