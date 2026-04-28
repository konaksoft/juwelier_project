# -*- coding: utf-8 -*-
"""
Created on Thu Dec 26 22:12:51 2019

@author: Serdar Ateş
"""
'''import base64

from Crypto import Random
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from Crypto.Util.Padding import unpad'''
from cryptography.fernet import Fernet


class AESCipher(object):
    def __init__(self, key):
        self.key = b'ZRS6NHbYkINcP2Z0Wz6U0yeaxXRpWX7y0FB7AULV4ig='

    def encrypt(self, data):
        fernet = Fernet(self.key)
        enc_data = fernet.encrypt(data.encode())
        return enc_data

    def decrypt(self, data):
        fernet = Fernet(self.key)
        dec_data = fernet.decrypt(data).decode()
        return dec_data

    '''def __init__(self, key):
        self.bs = AES.block_size
        self.key = hashlib.sha256(key.encode()).digest()

    def encrypt(self, raw):
        raw = self._pad(raw)
        iv = Random.new().read(AES.block_size)
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return base64.b64encode(iv + cipher.encrypt(raw.encode()))

    def decrypt(self, enc):
        enc = base64.b64decode(enc)
        iv = enc[:AES.block_size]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return self._unpad(cipher.decrypt(enc[AES.block_size:])).decode('utf-8')

    def _pad(self, s):
        return s + (self.bs - len(s) % self.bs) * chr(self.bs - len(s) % self.bs)

    @staticmethod
    def _unpad(s):
        return s[:-ord(s[len(s) - 1:])]
'''