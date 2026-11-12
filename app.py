from urllib.parse import urljoin
from flask import Flask, Response, abort, request
import requests
from cachetools import TTLCache

app = Flask(__name__)

master_link_cache = TTLCache(maxsize=10, ttl=3600)
BASE_STREAM_URL = "https://live.duhnet.tv/S2/HLS_LIVE/cnnturknp/"


def get_m3u8_link(x_forwarded_for):
  cache_key = f"master_link_{x_forwarded_for}"
  if cache_key in master_link_cache:
    return master_link_cache[cache_key]

  api_url = (
      "https://www.cnnturk.com/api/cnnvideo/media?id=62d6814670380e2cdc7c124c&isMobile=true"
  )
  headers = {"X-Forwarded-For": x_forwarded_for}

  try:
    response = requests.get(api_url, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    service_url = data["Media"]["Link"]["ServiceUrl"]
    secure_path = (
        data["Media"]["Link"]["SecurePath"]
        .encode("utf-8")
        .decode("unicode_escape")
    )
    final_link = f"{service_url}{secure_path}"

    master_link_cache[cache_key] = final_link
    return final_link
  except Exception as e:
    raise RuntimeError(f"API bağlantı hatası: {e}")


@app.route("/cnnturk/master.m3u8")
def master_playlist():
  client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

  try:
    upstream_master_url = get_m3u8_link(client_ip)

    # Gelen query parametrelerini master isteğine de ekleyelim
    resp = requests.get(
        upstream_master_url, params=request.args, timeout=10
    )
    resp.raise_for_status()
    master_content = resp.text

    lines = master_content.splitlines()
    new_lines = []
    base_host_url = request.host_url.rstrip("/")

    # Mevcut query string'i al (örn: ?live=true&app=...)
    query_string = (
        f"?{request.query_string.decode('utf-8')}"
        if request.query_string
        else ""
    )

    for line in lines:
      line = line.strip()
      if line and not line.startswith("#"):
        # Alt linke de query parametrelerini taşıyoruz
        proxy_url = f"{base_host_url}/cnnturk/{line}"
        new_lines.append(proxy_url)
      else:
        new_lines.append(line)

    modified_content = "\n".join(new_lines)
    return Response(modified_content, mimetype="application/vnd.apple.mpegurl")

  except Exception as e:
    return f"Error generating master playlist: {e}", 500


@app.route("/cnnturk/<path:file_name>")
def proxy_handler(file_name):
  if not (file_name.endswith(".m3u8") or file_name.endswith(".ts")):
    abort(403, description="Yetkisiz dosya uzantısı!")

  target_url = urljoin(BASE_STREAM_URL, file_name)

  try:
    # KRİTİK: İstemciden gelen query parametrelerini (st, e, live vb.) upstream'e iletiyoruz
    resp = requests.get(
        target_url, params=request.args, stream=True, timeout=15
    )
    resp.raise_for_status()

    if file_name.endswith(".m3u8"):
      playlist_content = resp.text
      lines = playlist_content.splitlines()
      new_lines = []
      base_host_url = request.host_url.rstrip("/")
      query_string = (
          f"?{request.query_string.decode('utf-8')}"
          if request.query_string
          else ""
      )

      for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
          absolute_segment_url = urljoin(target_url, line)
          relative_path = absolute_segment_url.replace(BASE_STREAM_URL, "", 1)
          # Segmentlere de aynı token/query parametrelerini ekliyoruz
          proxy_url = f"{base_host_url}/cnnturk/{relative_path}"
          new_lines.append(proxy_url)
        else:
          new_lines.append(line)

      modified_playlist = "\n".join(new_lines)
      return Response(
          modified_playlist, mimetype="application/vnd.apple.mpegurl"
      )

    else:
      def generate():
        for chunk in resp.iter_content(chunk_size=128 * 1024):
          if chunk:
            yield chunk

      content_type = resp.headers.get("Content-Type", "video/mp2t")
      return Response(
          generate(), mimetype=content_type, status=resp.status_code
      )

  except Exception as e:
    return f"Proxy akış hatası: {e}", 502


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000, debug=True)
