#!/usr/bin/env python3
"""Assemble each UI variant into one self-contained chat_ui HTML file.

Every variant shares body.html and core.js - only the stylesheet differs - so
whichever one is chosen, the behaviour and the API contract are identical to
what the flowgraph already serves.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, '..'))

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' "
           "fill='{bg}'/%3E%3Cpath d='M3 12h3l2.5-6 3.5 12 2.5-7 1.5 3h4' "
           "fill='none' stroke='{fg}' stroke-width='2' stroke-linecap='round'"
           "/%3E%3C/svg%3E")

VARIANTS = {
    'a': dict(css='a_studio.css',  name='Studio',     panel='open',
              icon=('%23ffffff', '%234f46e5')),
    'b': dict(css='b_instrument.css', name='Instrument', panel='open',
              icon=('%23ffffff', '%230d7a6f')),
    'c': dict(css='c_aurora.css',  name='Aurora',     panel='',
              icon=('%23ffffff', '%232563eb')),
    'd': dict(css='d_ledger.css',  name='Ledger',     panel='open',
              icon=('%23fbfaf6', '%2314634a')),
}

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BPSK Link Terminal</title>
<link rel="icon" href="__FAVICON__">
<style>
__STYLE__
</style>
</head>
<body>
__BODY__
<script>
const DEMO_IMAGE = "__DEMO_IMAGE__";
__CORE__
</script>
</body>
</html>
"""


def build(key, spec, single=None):
    body = open(os.path.join(HERE, 'body.html')).read()
    body = body.replace('__PANEL_CLASS__', spec['panel'])
    body = body.replace('__TOGGLE_CLASS__', 'on' if spec['panel'] else '')
    page = (TEMPLATE
            .replace('__FAVICON__', FAVICON.format(bg=spec['icon'][0],
                                                   fg=spec['icon'][1]))
            .replace('__STYLE__', open(os.path.join(HERE, spec['css'])).read())
            .replace('__BODY__', body)
            .replace('__DEMO_IMAGE__',
                     open(os.path.join(HERE, 'demo_image.txt')).read().strip())
            .replace('__CORE__', open(os.path.join(HERE, 'core.js')).read()))
    name = single or ('chat_ui_%s_%s.html' % (key, spec['name'].lower()))
    path = os.path.join(OUT, name)
    with open(path, 'w') as fh:
        fh.write(page)
    return path, len(page)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:                      # build one variant as chat_ui.html
        key = sys.argv[1].lower()
        path, size = build(key, VARIANTS[key], single='chat_ui.html')
        print('%-40s %6.1f kB  (%s)' % (path, size/1024, VARIANTS[key]['name']))
    else:
        for key, spec in VARIANTS.items():
            path, size = build(key, spec)
            print('%-46s %6.1f kB' % (os.path.basename(path), size/1024))
