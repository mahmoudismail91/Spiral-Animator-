
Spiral Animator (المحرك اللولبي) is an experimental 2D animation and drawing tool for desktop, created by Mahmoud Ismail (Al Hut الحوت). It's designed for creating frame-by-frame animations with added features for generative art.


![Screenshot of the Spiral Animator interface](https://raw.githubusercontent.com/mahmoudismail91/Spiral-Animator/main/animator-screenshot.png.jpg)


Features

2D Animation: A full frame-by-frame animation timeline.

Drawing Tools: Includes Pencil, Brush, Eraser, Spray Paint, and a fast Flood Fill.

Generative Drawing: Use the "Waver / Drunkenness" slider to add randomness to your brush strokes.

Image Thrower: Load a folder of images and "throw" them onto your canvas for a collage effect.

Audio Playback: Load a folder of .wav files. The app will randomly play tracks from the folder to help you animate to music.

Live A/V Recording: Record your animation as you draw (including audio) to an MP4 or GIF.

Timeline Export: Export your finished animation timeline to MP4, GIF, or a PNG image sequence.

Installation

This application is built with Python and PyQt6.

1. FFMPEG:
This application requires ffmpeg for exporting videos with audio.

Windows & Mac: Download from https://ffmpeg.org/download.html

Linux (Ubuntu/Debian): sudo apt update && sudo apt install ffmpeg

2. Python Dependencies:
You can install all the required Python libraries using the requirements.txt file.

# Clone this repository
git clone [https://github.com/mahmoudismail91/Spiral-Animator.git](https://github.com/mahmoudismail91/Spiral-Animator.git)
cd Spiral-Animator

# Install the required libraries
pip install -r requirements.txt


Usage

Once all dependencies are installed, you can run the application from your terminal:

python animator25.py


Use the Drawing Tools (Brush, Pencil, Fill, etc.) to draw on the canvas.

Click "➕ Add Frame" to add a new frame to your animation.

Click "▶️ Play" to loop your animation.

Load a Random Image Folder or Audio Folder in the "Random Assets & Audio" dock to add generative elements.

Go to File > Export As to save your work.

License

This project is licensed under the MIT License. (See LICENSE file).

Acknowledgements

Creator: Mahmoud Ismail (Al Hut الحوت)
