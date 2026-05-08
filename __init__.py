from calibre.customize import InterfaceActionBase


class CuriePlugin(InterfaceActionBase):
    name                    = 'Curie'
    description             = 'Generate spoiler-free character and location data for EPUB books using Claude AI'
    supported_platforms     = ['windows', 'osx', 'linux']
    author                  = 'Erik Fanki'
    version                 = (0, 1, 0)
    minimum_calibre_version = (5, 0, 0)
    actual_plugin           = 'calibre_plugins.curie.main:CurieAction'

    def is_customizable(self):
        return False
