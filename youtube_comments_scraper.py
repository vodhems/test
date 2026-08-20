"""Fetch top-level YouTube comments for a set of videos and export them to CSV.

Intended to run in Google Colab. Requires the `google-api-python-client`
package (`pip install google-api-python-client`) and a YouTube Data API v3 key.

Set your API key as the Colab secret / environment variable YOUTUBE_API_KEY
rather than hardcoding it in the script:

    import os
    os.environ["YOUTUBE_API_KEY"] = "your-key-here"

or, in Colab, use Secrets (the key icon in the left sidebar) and:

    from google.colab import userdata
    os.environ["YOUTUBE_API_KEY"] = userdata.get("YOUTUBE_API_KEY")
"""

import os

import pandas as pd
from googleapiclient.discovery import build

try:
    from google.colab import files
except ImportError:
    files = None

API_KEY = os.environ["YOUTUBE_API_KEY"]
OUTPUT_FILENAME = "eoselya_2026_comments.csv"

VIDEO_IDS = [
    "Fvfzc4dEXP8",
    "nnrXaubRyLU",
    "850nZ-tQOH4",
    "dHKzlQNdIjE",
    "JkzCo1-O3o0",
    "_8l9AoGugC0",
    "m-x8qfQJMvU",
]


def fetch_comments(youtube, video_id):
    comments = []
    request = youtube.commentThreads().list(
        part="snippet", videoId=video_id, maxResults=100, textFormat="plainText"
    )

    while request:
        response = request.execute()
        for item in response.get("items", []):
            top_comment = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "video_id": video_id,
                "author": top_comment.get("authorDisplayName", ""),
                "published_at": top_comment.get("publishedAt", ""),
                "likes": top_comment.get("likeCount", 0),
                "text": top_comment.get("textDisplay", ""),
            })

        request = youtube.commentThreads().list_next(request, response)

    return comments


def main():
    youtube = build("youtube", "v3", developerKey=API_KEY)
    all_comments = []

    print("Починаємо збір коментарів...")

    for video_id in VIDEO_IDS:
        print(f"Збір коментарів для відео {video_id}...")
        try:
            all_comments.extend(fetch_comments(youtube, video_id))
        except Exception as e:
            print(f"Помилка для {video_id}: {e}")

    if not all_comments:
        print("\nКоментарів не знайдено.")
        return

    df = pd.DataFrame(all_comments)
    df.to_csv(OUTPUT_FILENAME, index=False, encoding="utf-8-sig")
    print(f"\nЗібрано {len(all_comments)} коментарів у {OUTPUT_FILENAME}")

    if files is not None:
        files.download(OUTPUT_FILENAME)


if __name__ == "__main__":
    main()
