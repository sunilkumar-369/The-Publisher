import os
import sys
import time
import math
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from requests_oauthlib import OAuth1

VIDEO_PATH = "/tmp/editzone_upload.mp4"
META_API_VERSION = "v26.0"
LINKEDIN_API_VERSION = "202610"


def require_env(*keys):
    missing = [k for k in keys if not os.getenv(k, "").strip()]
    if missing:
        raise RuntimeError(f"Missing GitHub Secret(s): {', '.join(missing)}")


def download_from_google_drive(file_id: str, dest_path: str):
    """Downloads a shared MP4 file from Google Drive."""
    session = requests.Session()
    url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download", "confirm": "t"}
    response = session.get(url, params=params, stream=True, timeout=120)
    response.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"[Drive] Downloaded {size_mb:.2f} MB to {dest_path}")
    if size_mb < 0.05:
        raise RuntimeError("Downloaded file is too small—ensure Drive link is set to 'Anyone with the link'.")


def generate_caption_if_needed(topic: str, custom_caption: str) -> str:
    if custom_caption.strip():
        return custom_caption.strip()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return f"{topic} 🚀\n\nFollow @edit_zone_team for viral edits & AI prompts!\n#videoediting #aitools #contentcreator #reels #shorts"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = (
        f"Write a punchy social media caption for Edit Zone Team about: '{topic}'. "
        "Include a 1-line hook, 1 value takeaway, CTA 'Comment PROMPT for the workflow', and 5 hashtags. "
        "Strictly under 240 characters."
    )
    res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
    res.raise_for_status()
    return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


# 1. YOUTUBE SHORTS
def upload_youtube(topic: str, caption: str):
    require_env("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)

    title = f"{topic} #Shorts"[:98]
    body = {
        "snippet": {
            "title": title,
            "description": f"{caption}\n\n#Shorts",
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(VIDEO_PATH, mimetype="video/mp4", chunksize=4 * 1024 * 1024, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = req.next_chunk()
    return f"https://youtube.com/shorts/{response['id']}"


# 2. INSTAGRAM REELS (Resumable Binary Upload)
def upload_instagram(caption: str):
    require_env("IG_USER_ID", "META_ACCESS_TOKEN")
    ig_id = os.environ["IG_USER_ID"]
    token = os.environ["META_ACCESS_TOKEN"]
    file_size = os.path.getsize(VIDEO_PATH)

    init_res = requests.post(
        f"https://graph.facebook.com/{META_API_VERSION}/{ig_id}/media",
        params={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=30,
    ).json()
    if "id" not in init_res:
        raise RuntimeError(f"IG init failed: {init_res}")
    container_id = init_res["id"]

    with open(VIDEO_PATH, "rb") as f:
        up_res = requests.post(
            f"https://rupload.facebook.com/ig-api-upload/{META_API_VERSION}/{container_id}",
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=f,
            timeout=180,
        )
        up_res.raise_for_status()

    for _ in range(24):
        st = requests.get(
            f"https://graph.facebook.com/{META_API_VERSION}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=20,
        ).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"IG processing error: {st}")
        time.sleep(5)

    pub = requests.post(
        f"https://graph.facebook.com/{META_API_VERSION}/{ig_id}/media_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=30,
    ).json()
    if "id" not in pub:
        raise RuntimeError(f"IG publish failed: {pub}")
    return f"IG Media ID: {pub['id']}"


# 3. FACEBOOK PAGE REELS
def upload_facebook(caption: str):
    require_env("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN")
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]
    file_size = os.path.getsize(VIDEO_PATH)

    start = requests.post(
        f"https://graph.facebook.com/{META_API_VERSION}/{page_id}/video_reels",
        json={"upload_phase": "start", "access_token": token},
        timeout=30,
    ).json()
    if "video_id" not in start:
        raise RuntimeError(f"FB start failed: {start}")
    video_id = start["video_id"]
    upload_url = start["upload_url"]

    with open(VIDEO_PATH, "rb") as f:
        up = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=f,
            timeout=180,
        )
        up.raise_for_status()

    finish = requests.post(
        f"https://graph.facebook.com/{META_API_VERSION}/{page_id}/video_reels",
        params={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": token,
        },
        timeout=60,
    ).json()
    return f"FB Video ID: {video_id} ({finish.get('success')})"


# 4. LINKEDIN VIDEO POST
def upload_linkedin(topic: str, caption: str):
    require_env("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_AUTHOR_URN")
    token = os.environ["LINKEDIN_ACCESS_TOKEN"]
    author = os.environ["LINKEDIN_AUTHOR_URN"]
    file_size = os.path.getsize(VIDEO_PATH)
    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

    init_resp = requests.post(
        "https://api.linkedin.com/rest/videos?action=initializeUpload",
        headers=headers,
        json={
            "initializeUploadRequest": {
                "owner": author,
                "fileSizeBytes": file_size,
                "uploadCaptions": False,
                "uploadThumbnail": False,
            }
        },
        timeout=30,
    )
    init_resp.raise_for_status()
    init = init_resp.json()["value"]

    video_urn = init["video"]
    upload_token = init.get("uploadToken", "")
    etags = []

    with open(VIDEO_PATH, "rb") as f:
        for instr in init["uploadInstructions"]:
            f.seek(instr["firstByte"])
            chunk = f.read(instr["lastByte"] - instr["firstByte"] + 1)
            part_res = requests.put(
                instr["uploadUrl"],
                headers={"Content-Type": "application/octet-stream"},
                data=chunk,
                timeout=120,
            )
            part_res.raise_for_status()
            etags.append(part_res.headers["ETag"])

    requests.post(
        "https://api.linkedin.com/rest/videos?action=finalizeUpload",
        headers=headers,
        json={
            "finalizeUploadRequest": {
                "video": video_urn,
                "uploadToken": upload_token,
                "uploadedPartIds": etags,
            }
        },
        timeout=30,
    ).raise_for_status()

    for _ in range(20):
        st = requests.get(
            f"https://api.linkedin.com/rest/videos/{requests.utils.quote(video_urn, safe='')}",
            headers=headers,
            timeout=20,
        ).json()
        if st.get("status") == "AVAILABLE":
            break
        time.sleep(4)

    post_res = requests.post(
        "https://api.linkedin.com/rest/posts",
        headers=headers,
        json={
            "author": author,
            "commentary": caption,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED"},
            "content": {"media": {"title": topic[:100], "id": video_urn}},
            "lifecycleState": "PUBLISHED",
        },
        timeout=30,
    )
    post_res.raise_for_status()
    return f"LinkedIn Post ID: {post_res.headers.get('x-restli-id', 'Published')}"


# 5. X (TWITTER) CHUNKED VIDEO POST
def upload_x(caption: str):
    require_env("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    auth = OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    media_url = "https://upload.twitter.com/1.1/media/upload.json"
    file_size = os.path.getsize(VIDEO_PATH)

    init = requests.post(
        media_url,
        auth=auth,
        data={"command": "INIT", "total_bytes": file_size, "media_type": "video/mp4", "media_category": "tweet_video"},
        timeout=30,
    ).json()
    if "media_id_string" not in init:
        raise RuntimeError(f"X media INIT failed: {init}")
    media_id = init["media_id_string"]

    chunk_size = 4 * 1024 * 1024
    with open(VIDEO_PATH, "rb") as f:
        for seg in range(math.ceil(file_size / chunk_size)):
            chunk = f.read(chunk_size)
            requests.post(
                media_url,
                auth=auth,
                data={"command": "APPEND", "media_id": media_id, "segment_index": seg},
                files={"media": chunk},
                timeout=120,
            ).raise_for_status()

    fin = requests.post(media_url, auth=auth, data={"command": "FINALIZE", "media_id": media_id}, timeout=30).json()
    if "processing_info" in fin:
        for _ in range(20):
            st = requests.get(media_url, auth=auth, params={"command": "STATUS", "media_id": media_id}, timeout=20).json()
            state = st.get("processing_info", {}).get("state")
            if state == "succeeded":
                break
            if state == "failed":
                raise RuntimeError(f"X video processing failed: {st}")
            time.sleep(4)

    tweet = requests.post(
        "https://api.x.com/2/tweets",
        auth=auth,
        json={"text": caption[:280], "media": {"media_ids": [media_id]}},
        timeout=30,
    ).json()
    if "data" not in tweet:
        raise RuntimeError(f"X tweet failed: {tweet}")
    return f"https://x.com/i/status/{tweet['data']['id']}"


def main():
    file_id = os.environ["DRIVE_FILE_ID"]
    topic = os.environ["VIDEO_TOPIC"]
    custom_caption = os.getenv("CUSTOM_CAPTION", "")
    platforms = [p.strip().lower() for p in os.environ["PLATFORMS"].split(",") if p.strip()]

    download_from_google_drive(file_id, VIDEO_PATH)
    caption = generate_caption_if_needed(topic, custom_caption)
    print(f"[Caption Ready]\n{caption}\n" + "-" * 40)

    tasks = {
        "youtube": lambda: upload_youtube(topic, caption),
        "instagram": lambda: upload_instagram(caption),
        "facebook": lambda: upload_facebook(caption),
        "linkedin": lambda: upload_linkedin(topic, caption),
        "x": lambda: upload_x(caption),
    }

    errors = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {executor.submit(tasks[p]): p for p in platforms if p in tasks}
        for future in as_completed(future_map):
            plat = future_map[future]
            try:
                result = future.result()
                print(f"✅ [{plat.upper()}] Success -> {result}")
            except Exception as exc:
                print(f"❌ [{plat.upper()}] Failed -> {exc}")
                errors.append(plat)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
