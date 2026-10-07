{
    "name": "Buddy IA - Example mascot",
    "version": "18.0.1.0.0",
    "category": "Productivity",
    "summary": "Template: how a company adds its own mascot to Buddy IA without touching the core module",
    "author": "Equipo Buddy",
    "license": "LGPL-3",
    "depends": ["buddy_ia"],
    "assets": {
        "web.assets_backend": [
            "buddy_ia_mascot_example/static/src/scss/mascot_example.scss",
            "buddy_ia_mascot_example/static/src/xml/buddy_avatar_mascot_example.xml",
            "buddy_ia_mascot_example/static/src/js/mascot_example.js",
        ],
    },
    "installable": True,
    "application": False,
}
