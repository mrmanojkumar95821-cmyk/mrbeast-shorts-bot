import os
import time
import json
import logging
import tempfile
from flask import Flask, request, jsonify, send_file
import yt_dlp
import google.generativeai as genai
from moviepy.editor import VideoFileClip
from moviepy.video.fx.all import crop

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GENAI_API_KEY:
    genai.configure(api_key=GENAI_API_KEY)

def download_video(url, output_path):
    """Downloads video using yt-dlp."""
    ydl_opts = {
        'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
        # Use a common user agent to avoid bot detection
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path

def analyze_video_with_gemini(video_path):
    """Uploads video to Gemini and asks for viral segments."""
    logger.info("Uploading video to Gemini...")
    video_file = genai.upload_file(path=video_path)
    
    # Wait for processing
    while video_file.state.name == "PROCESSING":
        time.sleep(2)
        video_file = genai.get_file(video_file.name)

    if video_file.state.name == "FAILED":
        raise ValueError("Gemini video processing failed.")

    logger.info("Video processed. Generating content...")
    model = genai.GenerativeModel(model_name="gemini-1.5-flash")
    
    prompt = """
    Analyze this video and identify the ONE most viral/interesting segment suitable for a YouTube Short (vertical video).
    The segment should be between 30 and 60 seconds long.
    
    Return a JSON object with the following fields:
    - start_time: (float) Start time in seconds.
    - end_time: (float) End time in seconds.
    - title: (string) A catchy title for the short.
    - description: (string) A short description.
    - reason: (string) Why this part is interesting.
    
    Example JSON:
    {
        "start_time": 120.5,
        "end_time": 155.0,
        "title": "Crazy Stunt!",
        "description": "Watch this insane moment...",
        "reason": "High energy moment"
    }
    """
    
    response = model.generate_content([video_file, prompt], generation_config={"response_mime_type": "application/json"})
    
    # Cleanup file from Gemini
    genai.delete_file(video_file.name)
    
    try:
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Failed to parse Gemini response: {response.text}")
        raise e

def process_video_segment(input_path, output_path, start_time, end_time):
    """Crops video to 9:16 and cuts the segment."""
    clip = VideoFileClip(input_path).subclip(start_time, end_time)
    
    # Calculate crop for 9:16
    w, h = clip.size
    target_ratio = 9/16
    current_ratio = w/h
    
    if current_ratio > target_ratio:
        # Too wide, crop width
        new_width = h * target_ratio
        x_center = w / 2
        x1 = x_center - (new_width / 2)
        x2 = x_center + (new_width / 2)
        clip = crop(clip, x1=x1, y1=0, x2=x2, y2=h)
    else:
        # Too tall (unlikely for YT landscape), crop height
        new_height = w / target_ratio
        y_center = h / 2
        y1 = y_center - (new_height / 2)
        y2 = y_center + (new_height / 2)
        clip = crop(clip, x1=0, y1=y1, x2=w, y2=y2)
        
    # Write output
    clip.write_videofile(output_path, codec='libx264', audio_codec='aac', temp_audiofile='temp-audio.m4a', remove_temp=True)
    clip.close()

@app.route('/process-video', methods=['POST'])
def process_video_endpoint():
    data = request.json
    video_url = data.get('url')
    
    if not video_url:
        return jsonify({"error": "No URL provided"}), 400
        
    if not GENAI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    temp_dir = tempfile.mkdtemp()
    raw_video_path = os.path.join(temp_dir, "raw_video.mp4")
    final_video_path = os.path.join(temp_dir, "final_short.mp4")
    
    try:
        # 1. Download
        logger.info(f"Downloading {video_url}...")
        download_video(video_url, raw_video_path)
        
        # 2. Analyze
        logger.info("Analyzing...")
        analysis = analyze_video_with_gemini(raw_video_path)
        logger.info(f"Analysis result: {analysis}")
        
        start = analysis.get('start_time')
        end = analysis.get('end_time')
        
        if start is None or end is None:
            return jsonify({"error": "Could not determine start/end times"}), 500
            
        # 3. Process (Cut & Crop)
        logger.info("Processing video...")
        process_video_segment(raw_video_path, final_video_path, start, end)
        
        # 4. Return File
        # We also want to return the title/description. 
        # Since we can't easily return JSON + File in one go without multipart complexity that n8n might struggle with,
        # we will return the file and put metadata in custom headers.
        response = send_file(final_video_path, mimetype='video/mp4', as_attachment=True, download_name='short.mp4')
        response.headers['X-Video-Title'] = json.dumps(analysis.get('title', ''))
        response.headers['X-Video-Description'] = json.dumps(analysis.get('description', ''))
        
        return response

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500
    finally:
        # Cleanup is tricky with send_file as it needs the file to exist when sending.
        # In a real production app, we'd use a background task or a proper cleanup mechanism.
        # For this simple Render instance, the ephemeral container will eventually be recycled, 
        # but to avoid filling disk, we should try to clean up. 
        # Flask's send_file doesn't automatically delete. 
        # We'll leave it for now as Render restarts often.
        pass

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
