# -*- coding: utf-8 -*-
from dotenv import load_dotenv
import os

load_dotenv()  # .env を読み込む

def get_env(val:str):
    return os.getenv(val)