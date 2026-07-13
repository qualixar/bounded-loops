Output assets/demo.gif

Set Shell "bash"
Set FontSize 22
Set Width 1200
Set Height 680
Set Theme { "name": "bounded-loops", "black": "#0b1020", "green": "#4ade80", "red": "#fb7185" }

Type "./loops/bug-fix-red-green/wreck.sh"
Enter
Sleep 3s
Type "bl run loops/bug-fix-red-green --yes"
Enter
Sleep 4s

# The committed GIF is rendered from the real command output with:
#   python3 scripts/render_demo_gif.py
# This tape remains the terminal-scene specification for VHS users.
