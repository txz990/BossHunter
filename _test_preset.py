import urllib.request, urllib.parse

# 日日新 = E6 97 A5 E6 97 A5 E6 96 B0
correct_enc = "%E6%97%A5%E6%97%A5%E6%96%B0Deepseek"
url = "http://127.0.0.1:8686/api/ai/presets/detail?name=" + correct_enc
try:
    r = urllib.request.urlopen(url)
    print("OK", r.status, r.read().decode()[:80])
except urllib.error.HTTPError as e:
    print("ERR", e.code, e.read().decode()[:200])
