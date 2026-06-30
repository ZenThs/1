import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

class Loader:
    def __init__(self, *args, **kwargs):
        pass
    def start(self):
        pass
    def stop(self):
        pass
    
    # In some places Loader.stop() is called as a staticmethod inside except blocks
    # So let's make it work on class level as well via a classmethod
    # but since it's sometimes an instance method, python will pass self if it's not a classmethod/staticmethod
    # Let's just catch it or make it safely callable.
    # Actually if they do `Loader.stop()` on the class, a static method is needed.
    
    @classmethod
    def stop(cls):
        pass


class Logger:
    def __init__(self):
        self.logger = logging.getLogger("reCAPTCHA")
    
    def info(self, msg):
        self.logger.info(msg)
    
    def debug(self, msg):
        self.logger.debug(msg)
    
    def warning(self, msg):
        self.logger.warning(msg)
    
    def error(self, msg):
        self.logger.error(msg)
    
    def failure(self, msg):
        self.logger.error(msg)
    
    def success(self, msg):
        self.logger.info(f"SUCCESS: {msg}")
    
    def message(self, title, msg, start=None, end=None):
        time_str = f" in {end-start:.2f}s" if start and end else ""
        self.logger.info(f"[{title}] {msg}{time_str}")
        
    def question(self, msg):
        return input(msg)
