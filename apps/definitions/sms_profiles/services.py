import requests
import json
import base64
from django.conf import settings


class NetgsmService:
    def __init__(self):
        # Dokümandaki OTP URL'si
        self.otp_url = "https://api.netgsm.com.tr/sms/rest/v2/otp"

        # Ayarlar
        self.usercode = str(getattr(settings, 'NETGSM_USERCODE', '')).strip()
        self.password = str(getattr(settings, 'NETGSM_PASSWORD', '')).strip()
        self.header = str(getattr(settings, 'NETGSM_HEADER', '')).strip()

    def send_otp(self, phone, code):
        """
        Netgsm OTP Servisi (v2/otp) - Basic Auth Yöntemi
        """
        if not phone:
            return {"result": False, "msg": "Telefon numarası eksik."}

        # 1. Telefon Numarasını Temizle (5xxxxxxxxx formatı)
        clean_phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if clean_phone.startswith("+90"):
            clean_phone = clean_phone[3:]
        elif clean_phone.startswith("90"):
            clean_phone = clean_phone[2:]
        elif clean_phone.startswith("0"):
            clean_phone = clean_phone[1:]

        # 2. Mesaj İçeriği
        message = f"Dogrulama kodunuz: {code}"

        # 3. Basic Authentication (Dokümana Uygun)
        # Kullanıcı adı ve şifre birleştirilip şifreleniyor
        credentials = f"{self.usercode}:{self.password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Basic {encoded_credentials}'
        }

        # 4. İstek Gövdesi (Dokümana Uygun - Tekil Mesaj)
        payload = {
            "msgheader": self.header,
            "appname": "KuyumPlus",
            "msg": message,
            "no": clean_phone
        }

        try:
            # POST İsteği
            response = requests.post(self.otp_url, data=json.dumps(payload), headers=headers, timeout=15)

            try:
                resp_json = response.json()
            except json.JSONDecodeError:
                return {"result": False, "msg": f"API yanıtı okunamadı: {response.text}"}

            # Dokümana göre başarı kodu "00"
            if resp_json.get("code") == "00":
                return {
                    "result": True,
                    "msg": "SMS başarıyla gönderildi (OTP).",
                    "job_id": resp_json.get("jobid")
                }
            else:
                # Hata Durumu
                error_desc = resp_json.get("description", "Bilinmeyen Hata")
                error_code = resp_json.get("code", "N/A")
                return {
                    "result": False,
                    "msg": f"Netgsm OTP Hatası: {error_code} - {error_desc}"
                }

        except Exception as e:
            return {"result": False, "msg": f"Bağlantı Hatası: {str(e)}"}
