import pyperclip

class ClipboardPlugin:
    """A modular plugin for copying and pasting data to/from the system clipboard."""
    
    def __init__(self):
        self.plugin_name = "Clipboard Manager"
        self.version = "1.0.0"

    def execute_copy(self, text_to_copy: str) -> bool:
        """Copies the provided text string to the system clipboard."""
        try:
            if not isinstance(text_to_copy, str):
                text_to_copy = str(text_to_copy)
            
            pyperclip.copy(text_to_copy)
            print(f"[{self.plugin_name}] Successfully copied text to clipboard.")
            return True
        except Exception as e:
            print(f"[{self.plugin_name}] Copy failed: {e}")
            return False

    def execute_paste(self) -> str:
        """Retrieves and returns text currently stored in the system clipboard."""
        try:
            pasted_text = pyperclip.paste()
            print(f"[{self.plugin_name}] Successfully retrieved text from clipboard.")
            return pasted_text
        except Exception as e:
            print(f"[{self.plugin_name}] Paste failed: {e}")
            return ""

# Hook required for dynamic plugin loaders to detect it
def register_plugin():
    return ClipboardPlugin()
