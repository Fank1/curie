from calibre.utils.config import JSONConfig

prefs = JSONConfig('plugins/curie')
prefs.defaults['api_key']             = ''
prefs.defaults['model']               = 'claude-sonnet-4-6'
prefs.defaults['include_characters']  = True
prefs.defaults['include_places']      = False
prefs.defaults['language']            = 'English'
prefs.defaults['target_reader']       = 'koreader'
prefs.defaults['hint_density']        = 'every_10_paragraphs'
prefs.defaults['provider']            = 'anthropic'
prefs.defaults['ollama_host']         = 'http://localhost:11434'
prefs.defaults['ollama_model']        = ''
prefs.defaults['ollama_context_size'] = 8192
