from app import SpectroApp
import tkinter as tk
from PIL import Image, ImageTk
import os

def show_splash_screen():
    """Display splash screen with SciGlob logo with fade-in effect."""
    splash = tk.Tk()
    splash.title("SciGlob")
    splash.overrideredirect(True)  # Remove window decorations

    # Get screen dimensions
    screen_width = splash.winfo_screenwidth()
    screen_height = splash.winfo_screenheight()

    # Scale splash to screen -- works on small portables too
    splash_width = min(680, int(screen_width * 0.45))
    splash_height = min(420, int(screen_height * 0.45))

    x = (screen_width - splash_width) // 2
    y = (screen_height - splash_height) // 2
    splash.geometry(f"{splash_width}x{splash_height}+{x}+{y}")

    bg_color = "#0f172a"  # deep slate
    splash.configure(bg=bg_color)
    splash.attributes("-alpha", 0.0)

    # Load and display logo
    try:
        logo_path = os.path.join(os.path.dirname(__file__), "sciglob_logoRGB.png")
        img = Image.open(logo_path)
        img.thumbnail((int(splash_width * 0.82), int(splash_height * 0.72)),
                       Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)

        logo_label = tk.Label(splash, image=photo, bg=bg_color,
                              borderwidth=0, highlightthickness=0)
        logo_label.image = photo
        logo_label.pack(expand=True, pady=(20, 4))
    except Exception as e:
        label = tk.Label(splash, text="SciGlob\nSpectrometer Characterization System",
                         font=("Segoe UI", 20, "bold"), bg=bg_color, fg="#60a5fa",
                         borderwidth=0, highlightthickness=0)
        label.pack(expand=True)
        print(f"Could not load logo: {e}")

    # Subtle version / tagline
    tag = tk.Label(splash, text="Spectrometer Characterization System",
                   font=("Segoe UI", 10), bg=bg_color, fg="#64748b",
                   borderwidth=0, highlightthickness=0)
    tag.pack(pady=(0, 18))

    splash.update()

    # Smooth fade-in
    def fade_in(alpha=0.0):
        if alpha < 1.0:
            alpha += 0.04
            splash.attributes("-alpha", alpha)
            splash.after(25, lambda: fade_in(alpha))

    fade_in()
    splash.after(2500, splash.destroy)
    splash.mainloop()

if __name__ == '__main__':
    # Show splash screen
    show_splash_screen()
    
    # Create and run main application
    app = SpectroApp()
    app.mainloop()
