"""Explicit mapping of whisper.cpp 1.9.5 detector-supported language codes.

Only codes present in whisper.cpp's g_lang table are mapped. The automatic
exclusion policy is deliberately explicit:

* English, Chinese (including Cantonese) and Korean are allowed.
* The listed European languages and clearly regional European languages are
  eligible for automatic exclusion when the detector is confident.
* Every other supported language is unauthorized for automatic exclusion by
  this instruction, so it stays eligible.
* Irish (ga) is not in whisper.cpp 1.9.5's supported table and therefore
  cannot be detector-identified by this runtime.
"""

SUPPORTED = {
    "en": "English", "zh": "Chinese", "de": "German", "es": "Spanish",
    "ru": "Russian", "ko": "Korean", "fr": "French", "ja": "Japanese",
    "pt": "Portuguese", "tr": "Turkish", "pl": "Polish", "ca": "Catalan",
    "nl": "Dutch", "ar": "Arabic", "sv": "Swedish", "it": "Italian",
    "id": "Indonesian", "hi": "Hindi", "fi": "Finnish", "vi": "Vietnamese",
    "he": "Hebrew", "uk": "Ukrainian", "el": "Greek", "ms": "Malay",
    "cs": "Czech", "ro": "Romanian", "da": "Danish", "hu": "Hungarian",
    "ta": "Tamil", "no": "Norwegian", "th": "Thai", "ur": "Urdu",
    "hr": "Croatian", "bg": "Bulgarian", "lt": "Lithuanian", "la": "Latin",
    "mi": "Maori", "ml": "Malayalam", "cy": "Welsh", "sk": "Slovak",
    "te": "Telugu", "fa": "Persian", "lv": "Latvian", "bn": "Bengali",
    "sr": "Serbian", "az": "Azerbaijani", "sl": "Slovenian", "kn": "Kannada",
    "et": "Estonian", "mk": "Macedonian", "br": "Breton", "eu": "Basque",
    "is": "Icelandic", "hy": "Armenian", "ne": "Nepali", "mn": "Mongolian",
    "bs": "Bosnian", "kk": "Kazakh", "sq": "Albanian", "sw": "Swahili",
    "gl": "Galician", "mr": "Marathi", "pa": "Punjabi", "si": "Sinhala",
    "km": "Khmer", "sn": "Shona", "yo": "Yoruba", "so": "Somali",
    "af": "Afrikaans", "oc": "Occitan", "ka": "Georgian", "be": "Belarusian",
    "tg": "Tajik", "sd": "Sindhi", "gu": "Gujarati", "am": "Amharic",
    "yi": "Yiddish", "lo": "Lao", "uz": "Uzbek", "fo": "Faroese",
    "ht": "Haitian Creole", "ps": "Pashto", "tk": "Turkmen", "nn": "Nynorsk",
    "mt": "Maltese", "sa": "Sanskrit", "lb": "Luxembourgish", "my": "Myanmar",
    "bo": "Tibetan", "tl": "Tagalog", "mg": "Malagasy", "as": "Assamese",
    "tt": "Tatar", "haw": "Hawaiian", "ln": "Lingala", "ha": "Hausa",
    "ba": "Bashkir", "jw": "Javanese", "su": "Sundanese", "yue": "Cantonese",
}

ALLOWED = {"en", "zh", "yue", "ko"}

EUROPEAN_EXCLUDED = {
    "es", "pt", "fr", "de", "it", "nl", "pl", "cs", "sk", "hu", "ro", "bg",
    "el", "ru", "uk", "be", "sv", "no", "da", "fi", "is", "et", "lv", "lt",
    "hr", "sr", "bs", "sl", "sq", "mk", "ca", "gl", "eu", "cy", "mt", "br",
    "oc", "fo", "nn", "lb", "yi",
}

EXCLUDED = EUROPEAN_EXCLUDED
UNAUTHORIZED = set(SUPPORTED) - ALLOWED - EXCLUDED


def language_name(code):
    return SUPPORTED.get(code, code or "unknown")


def classify(code):
    if code in ALLOWED:
        return "allowed"
    if code in EXCLUDED:
        return "excluded"
    if code in SUPPORTED:
        return "unauthorized"
    return "unknown"


def is_excluded_code(code):
    return code in EXCLUDED


def is_supported_code(code):
    return code in SUPPORTED
