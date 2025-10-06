
class Config:
    # Приоритет источников: от высокого к низкому
    SOURCE_PRIORITY = ['imgur5', 'freeimage', 'kappa', 'pastenow', 'prnt', 'imgur7']

    # Сбалансированные квоты
    SOURCE_WEIGHTS = {
        'imgur5': 0.1,
        'imgur7': 0.2,
        'prnt': 0.1,
        'pastenow': 0.2,
        'freeimage': 0.2,
        'kappa': 0.2
    }

    BATCH_SIZES = {
        'imgur5': 10,
        'imgur7': 5,
        'prnt': 10,
        'pastenow': 10,
        'freeimage': 10,
        'kappa': 10
    }

    SOURCE_TIMEOUT = 600
    DNS_ERROR_TIMEOUT = 1800

    MAX_GROUP_SIZE = 10
    GROUP_TIMEOUT = 60

    STATUS_UPDATE_INTERVAL = 10
    UPDATE_ON_FOUND = 5
    UPDATE_ON_CHECKED = 50

    MEDIA_SEND_TIMEOUT = 60
    MAX_CONCURRENT_TASKS = 30
    MAX_RETRIES = 3
    COOLDOWN_DURATION = 180
    REQUEST_TIMEOUT = 15
    MAX_FILE_SIZE = 49 * 1024 * 1024
    LARGE_FILE_IMAGE = "file50mb.png"
    MIN_WIDTH = 30
    MIN_HEIGHT = 30

    FILE_SIGNATURES = {
        b'\xFF\xD8\xFF': 'jpg',
        b'\x89PNG\r\n\x1a\n': 'png',
        b'GIF87a': 'gif',
        b'GIF89a': 'gif',
        b'BM': 'bmp',
        b'RIFF....WEBPVP8': 'webp',
        b'PK\x03\x04': 'zip',
        b'Rar!\x1a\x07': 'rar',
        b'\x1F\x8B\x08': 'gz',
        b'7z\xBC\xAF\x27\x1C': '7z',
        b'\x25\x50\x44\x46': 'pdf',
        b'\x50\x4B\x03\x04': 'docx',
        b'\xD0\xCF\x11\xE0': 'doc',
        b'\x49\x44\x33': 'mp3',
        b'\xFF\xFB': 'mp3',
        b'\xFF\xF3': 'mp3',
        b'\xFF\xF2': 'mp3',
        b'fLaC': 'flac',
        b'OggS': 'ogg',
        b'\x1A\x45\xDF\xA3': 'webm',
        b'\x52\x49\x46\x46....\x57\x45\x42\x50': 'webp',
        b'\x52\x49\x46\x46....\x41\x56\x49\x20': 'avi',
        b'\x00\x00\x00 ftyp': 'mp4',
        b'\x00\x00\x00\x18ftyp': 'mp4',
        b'\x00\x00\x00\x20ftyp': 'mp4',
    }