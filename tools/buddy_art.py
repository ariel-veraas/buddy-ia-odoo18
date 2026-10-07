"""Arte de los buddys (SVG para OWL), generado desde un solo lugar.

Salida (todo en odoo-addons/buddy_ia/static/src/xml/, cada archivo es un template OWL):
  buddy_avatar.xml          el contenedor `buddy_ia.Avatar` (elige el buddy), los efectos de los gestos y los accesorios
  buddy_avatar_kitsune.xml  `buddy_ia.Avatar.kitsune`  el zorrito espíritu
  buddy_avatar_mochi.xml    `buddy_ia.Avatar.mochi`    la bolita blanda

Reglas del arte (para agregar un buddy nuevo, copiá uno de estos):
  * El template del buddy es `buddy_ia.Avatar.<id>` y su raíz es un <g xmlns="http://www.w3.org/2000/svg">: sin ese
    xmlns, OWL crea los nodos de un t-call con el espacio de nombres equivocado y no se ven.
  * Lienzo de 200 x 260. La cabeza queda centrada en (100, 84) con radio ~54: el lanzador la recorta en un círculo.
  * Colores: SOLO variables CSS (clases c-* de buddy_avatar.scss o style="...: var(--bd-...)"), nunca hex del admin.
  * Ids de gradientes/filtros: siempre con {{uid}}, el avatar aparece varias veces en pantalla.
  * Cara: un bloque por cada nombre de `FACES` (las 6 expresiones del contrato + las caras de los gestos).
  * Grupos con nombre fijo, que usan el CSS y los gestos: bd-char (todo el personaje), bd-look > bd-eyes + bd-brows,
    bd-mouth, bd-ring, bd-arm, y los propios de cada buddy (bd-tail, bd-ear-l/r, bd-leaf...).

Uso: python tools/buddy_art.py   (lo llama también tools/gen_avatar.py)
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XML_DIR = ROOT / "odoo-addons/buddy_ia/static/src/xml"
NS = 'xmlns="http://www.w3.org/2000/svg"'
INK = "#141a3c"

# Las 6 expresiones del contrato con la IA, y las caras extra que usan los gestos del cliente.
FACES = ["neutral", "happy", "thinking", "surprised", "doubt", "wink",
         "sparkle", "heart", "tears", "dizzy", "mad", "yawn", "sleep", "squint"]


def _id(name):
    return f't-attf-id="bd-{name}-{{{{uid}}}}"'


def _url(attr, name):
    return f't-attf-{attr}="url(#bd-{name}-{{{{uid}}}})"'


def chain(blocks, default="neutral", var="face"):
    """Un bloque por cara, como t-if / t-elif / t-else sobre la variable `face` del componente."""
    out, first = [], True
    for name in FACES:
        if name == default:
            continue
        tag = "t-if" if first else "t-elif"
        out.append(f'    <t {tag}="{var} === \'{name}\'">{blocks[name]}</t>')
        first = False
    out.append(f'    <t t-else="">{blocks[default]}</t>')
    return "\n".join(out)


def template(name, body):
    return f'  <t t-name="{name}">\n{body}\n  </t>\n'


def wrap(templates, note):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<templates xml:space="preserve">\n'
            f'  <!-- GENERADO por tools/buddy_art.py ({note}): no editar a mano, cambiá el generador. -->\n'
            + "".join(templates) + "</templates>\n")


def star(x, y, r, fill="#fff3a8"):
    """Estrella de 4 puntas (brillo de anime)."""
    k = r * 0.22
    return (f'<path d="M{x} {y - r} Q{x + k} {y - k} {x + r} {y} Q{x + k} {y + k} {x} {y + r} '
            f'Q{x - k} {y + k} {x - r} {y} Q{x - k} {y - k} {x} {y - r}Z" fill="{fill}"/>')


def heart(x, y, s, cls="", fill="#ff5c8a", stroke=INK, sw=2.2):
    # El transform va en un <g> aparte: la animación CSS de la clase pisaría el atributo transform del propio path.
    return (f'<g transform="translate({x} {y}) scale({s})"><path class="{cls}" '
            f'd="M0 7 C-13 -2 -9 -12 0 -5.5 C9 -12 13 -2 0 7Z" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{sw / s:.2f}" stroke-linejoin="round"/></g>')


def spiral(x, y, r, stroke=INK, sw=2.6, cls="bd-spiral"):
    """Espiral de mareo hecha con semicírculos que crecen."""
    u = r / 7.5
    d = (f"M{x} {y} a{1.5 * u:.2f} {1.5 * u:.2f} 0 0 1 {3 * u:.2f} 0 a{3 * u:.2f} {3 * u:.2f} 0 0 1 {-6 * u:.2f} 0 "
         f"a{4.5 * u:.2f} {4.5 * u:.2f} 0 0 1 {9 * u:.2f} 0 a{6 * u:.2f} {6 * u:.2f} 0 0 1 {-12 * u:.2f} 0 "
         f"a{7.5 * u:.2f} {7.5 * u:.2f} 0 0 1 {15 * u:.2f} 0")
    return (f'<g transform="translate({-1.5 * u:.2f} 0)"><path class="{cls}" d="{d}" fill="none" stroke="{stroke}" '
            f'stroke-width="{sw}" stroke-linecap="round"/></g>')


# ================================================================================================ Indicador de estado
def status_orb(x, y):
    """Indicador de estado de los buddys sin disco: una gotita de luz junto al personaje (en vez de un aro).
    El color lo pone el CSS segun la clase ring-* del avatar (idle: arcoiris, listening, thinking, ok, doubt)."""
    return (f'<g class="bd-status" transform="translate({x} {y})"><circle class="bd-dot" cx="0" cy="0" r="10.5" '
            f'{_url("fill", "rainbow")} stroke="{INK}" stroke-width="2.4"/>'
            f'<circle cx="-3.2" cy="-3.4" r="3" fill="#fff" opacity=".85"/></g>')


# ================================================================================================ Kitsune
def mirror(path):
    """Espeja (x -> 200 - x) un path con comandos absolutos M/L/C/Q/Z y pares de coordenadas."""
    tokens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", path)
    out, i = [], 0
    while i < len(tokens):
        if tokens[i].isalpha():
            out.append(tokens[i])
            i += 1
        else:
            out.append(f"{200 - float(tokens[i]):g}")
            out.append(tokens[i + 1])
            i += 2
    return " ".join(out)


K_HEAD = ("M100 49 C128 49 148 62 152 84 L164 92 L152 99 L160 112 L142 116 C132 130 116 136 100 136 "
          "C84 136 68 130 58 116 L40 112 L48 99 L36 92 L48 84 C52 62 72 49 100 49 Z")
K_EAR_OUT = "M52 82 Q44 46 54 14 Q57 9 63 12 Q90 26 92 52 Z"
K_EAR_IN = "M58 70 Q53 44 58 27 Q60 23 64 25 Q82 35 85 55 Z"
K_TAIL = "M112 122 C142 136 178 126 187 94 C191 77 187 58 175 44 C177 66 165 86 146 98 C136 104 124 106 112 106 Z"
K_TAIL_TIP = "M158 34 L200 34 L200 80 L190 72 L186 80 L178 68 L172 78 L164 62 Z"
K_BLUSH = '<ellipse cx="73" cy="110" rx="9" ry="5.5" class="c-blush"/><ellipse cx="127" cy="110" rx="9" ry="5.5" class="c-blush"/>'


def k_eye(cx, cy, side, rx=10.5, ry=13.5, look=(0, 0), hi="dot", pupil=(5.4, 7.6)):
    x, y = cx + look[0], cy + look[1]
    prx, pry = pupil
    glint = (star(x - 3.2, y - 4.6, 5.2, "#fff") if hi == "star"
             else f'<circle cx="{x - 3.4}" cy="{y - 4.6}" r="4" fill="#fff"/>')
    extra = ""
    if hi == "star":
        extra = f'<circle cx="{x + 4}" cy="{y + 5.5}" r="2.2" fill="#fff"/><circle cx="{x - 4.8}" cy="{y + 4.2}" r="1.2" fill="#fff" opacity=".9"/>'
    elif hi == "wet":
        extra = (f'<ellipse cx="{x}" cy="{y + ry * 0.62}" rx="{rx * 0.7}" ry="2.6" fill="#fff" opacity=".55"/>'
                 f'<circle cx="{x + 4.2}" cy="{y + 3.6}" r="2.1" fill="#fff"/>')
    else:
        extra = f'<circle cx="{x + 3.6}" cy="{y + 5.4}" r="1.9" fill="#fff" opacity=".92"/>'
    lash = (f'<path d="M{x - rx - 1.2:g} {y - ry * 0.3:g} Q{x - rx * 0.75:g} {y - ry - 2.2:g} {x:g} {y - ry - 1.6:g} '
            f'Q{x + rx * 0.75:g} {y - ry - 2.2:g} {x + rx + 1.2:g} {y - ry * 0.3:g}" fill="none" stroke="{INK}" '
            f'stroke-width="3.4" stroke-linecap="round"/>'
            f'<path d="M{x + side * (rx + 1.2):g} {y - ry * 0.3:g} l{side * 4:g} -2.6" fill="none" stroke="{INK}" '
            f'stroke-width="3" stroke-linecap="round"/>')
    return (f'<g class="bd-eye"><ellipse cx="{x:g}" cy="{y:g}" rx="{rx}" ry="{ry}" {_url("fill", "iris")}/>'
            f'<ellipse cx="{x:g}" cy="{y + 1:g}" rx="{prx}" ry="{pry}" fill="#2a1232"/>{glint}{extra}{lash}</g>')


def k_arc(cx, cy, w=11, up=True, sw=4.6):
    """Ojo cerrado: ^ (contento) o ‿ (dormido)."""
    c = cy - 16 if up else cy + 8
    return (f'<path d="M{cx - w} {cy + (3 if up else -4)} Q{cx} {c} {cx + w} {cy + (3 if up else -4)}" fill="none" '
            f'stroke="{INK}" stroke-width="{sw}" stroke-linecap="round"/>')


def k_brow(cx, y, tilt=0, w=7):
    return (f'<path d="M{cx - w} {y + tilt} Q{cx} {y - 4} {cx + w} {y - tilt}" fill="none" class="s-warm-dk" '
            f'stroke-width="3.2" stroke-linecap="round"/>')


K_NOSE = f'<path d="M95.5 107.5 Q100 105 104.5 107.5 Q102.5 112.4 100 113 Q97.5 112.4 95.5 107.5Z" fill="{INK}"/>'
K_W = f'<path d="M100 113 Q95 120 89 115 M100 113 Q105 120 111 115" fill="none" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'


def k_open_mouth(cy=118, rx=9, ry=11, tongue=True):
    t = f'<ellipse cx="100" cy="{cy + ry * 0.55:g}" rx="{rx * 0.62:g}" ry="{ry * 0.4:g}" fill="#ff8aa8"/>' if tongue else ""
    return (f'<path d="M{100 - rx} {cy - ry * 0.4:g} Q100 {cy + ry * 1.4:g} {100 + rx} {cy - ry * 0.4:g} Z" fill="#5a1730" '
            f'stroke="{INK}" stroke-width="2.4" stroke-linejoin="round"/>' + t)


def kitsune_faces():
    L, R, Y = 76, 124, 96

    def face(eyes, brows, mouth, extra=""):
        return (f'<g class="bd-look"><g class="bd-eyes">{eyes}</g><g class="bd-brows">{brows}</g></g>'
                f'{K_NOSE}<g class="bd-mouth">{mouth}</g>{extra}')

    open2 = k_eye(L, Y, -1) + k_eye(R, Y, 1)
    brows = k_brow(L, 76) + k_brow(R, 76)
    # Pestañas "dolor" cuando está triste y cejas de enojo.
    sad = (f'<path d="M66 80 L86 73" fill="none" class="s-warm-dk" stroke-width="3.2" stroke-linecap="round"/>'
           f'<path d="M134 80 L114 73" fill="none" class="s-warm-dk" stroke-width="3.2" stroke-linecap="round"/>')
    angry = (f'<path d="M64 74 L89 84" fill="none" stroke="{INK}" stroke-width="3.8" stroke-linecap="round"/>'
             f'<path d="M136 74 L111 84" fill="none" stroke="{INK}" stroke-width="3.8" stroke-linecap="round"/>')
    half_r = (f'<g class="bd-eye"><path d="M113.5 98 A10.5 13.5 0 0 0 134.5 98 Z" {_url("fill", "iris")}/>'
              f'<ellipse cx="124" cy="104" rx="5" ry="4.2" fill="#2a1232"/><circle cx="120.6" cy="102" r="2.6" fill="#fff"/>'
              f'<path d="M111.5 98 L136.5 98" stroke="{INK}" stroke-width="3.6" stroke-linecap="round"/>'
              f'<path d="M136.5 98 l4 -2.6" stroke="{INK}" stroke-width="3" stroke-linecap="round"/></g>')
    wavy = f'<path d="M89 118 Q94.5 112 100 118 T111 118" fill="none" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'
    return {
        "neutral": face(open2, brows, K_W),
        "happy": face(k_arc(L, Y) + k_arc(R, Y), k_brow(L, 74) + k_brow(R, 74), k_open_mouth(117, 8, 9),
                      '<ellipse cx="73" cy="110" rx="10" ry="6" class="c-blush" opacity=".35"/><ellipse cx="127" cy="110" rx="10" ry="6" class="c-blush" opacity=".35"/>'),
        "thinking": face(k_eye(L, Y, -1, look=(2.5, -3.2)) + k_eye(R, Y, 1, look=(2.5, -3.2)),
                         k_brow(L, 76) + f'<path d="M115 70 Q124 64 133 69" fill="none" class="s-warm-dk" stroke-width="3.2" stroke-linecap="round"/>',
                         f'<path d="M95 117 Q100 114 105 117" fill="none" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'),
        "surprised": face(k_eye(L, Y, -1, 11.5, 14.8, pupil=(4.4, 6)) + k_eye(R, Y, 1, 11.5, 14.8, pupil=(4.4, 6)),
                          k_brow(L, 68) + k_brow(R, 68), k_open_mouth(119, 5, 7, False)),
        "doubt": face(k_eye(L, Y, -1) + half_r, k_brow(L, 71) + f'<path d="M115 77 L133 80" fill="none" class="s-warm-dk" stroke-width="3.2" stroke-linecap="round"/>', wavy),
        "wink": face(k_arc(L, Y) + k_eye(R, Y, 1), k_brow(L, 72) + k_brow(R, 76), K_W),
        "sparkle": face(k_eye(L, Y, -1, 11.2, 14.2, hi="star") + k_eye(R, Y, 1, 11.2, 14.2, hi="star"),
                        k_brow(L, 72) + k_brow(R, 72), k_open_mouth(117, 8, 9)),
        "heart": face(heart(L, Y, 2.2, "bd-heart", "#ff5c8a", INK, 2.2) + heart(R, Y, 2.2, "bd-heart", "#ff5c8a", INK, 2.2),
                      k_brow(L, 74) + k_brow(R, 74), K_W, '<ellipse cx="73" cy="110" rx="10" ry="6" class="c-blush" opacity=".35"/><ellipse cx="127" cy="110" rx="10" ry="6" class="c-blush" opacity=".35"/>'),
        "tears": face(k_eye(L, Y, -1, hi="wet") + k_eye(R, Y, 1, hi="wet"), sad,
                      f'<path d="M92 120 Q100 113 108 120" fill="none" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>',
                      '<path class="bd-tear" d="M66 107 Q60 120 65 130 Q73 122 66 107Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.6"/>'
                      '<path class="bd-tear" d="M134 107 Q140 120 135 130 Q127 122 134 107Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.6"/>'),
        "dizzy": face(spiral(L, Y, 10.5) + spiral(R, Y, 10.5), "", wavy),
        "mad": face(f'<g class="bd-eye"><path d="M65.5 98 A10.5 12 0 0 0 86.5 98 L84 90 L68 85 Z" {_url("fill", "iris")}/>'
                    f'<ellipse cx="76" cy="100" rx="5" ry="5" fill="#2a1232"/><circle cx="72.8" cy="98" r="2.4" fill="#fff"/></g>'
                    f'<g class="bd-eye"><path d="M113.5 98 A10.5 12 0 0 0 134.5 98 L132 85 L116 90 Z" {_url("fill", "iris")}/>'
                    f'<ellipse cx="124" cy="100" rx="5" ry="5" fill="#2a1232"/><circle cx="120.8" cy="98" r="2.4" fill="#fff"/></g>',
                    angry, f'<path d="M91 121 Q100 112 109 121" fill="none" stroke="{INK}" stroke-width="2.8" stroke-linecap="round"/>'
                           f'<path d="M95 115.5 l2 3.4 l2 -3.4 M101 115.5 l2 3.4 l2 -3.4" fill="#fff" stroke="none"/>'),
        "yawn": face(k_arc(L, Y + 2, up=False, sw=4.2) + k_arc(R, Y + 2, up=False, sw=4.2), k_brow(L, 76, 1) + k_brow(R, 76, 1),
                     k_open_mouth(121, 10, 12),
                     '<path class="bd-tear" d="M62 100 Q59 106 62 110 Q65 106 62 100Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.4"/>'),
        "sleep": face(k_arc(L, Y + 2, up=False, sw=4.2) + k_arc(R, Y + 2, up=False, sw=4.2), k_brow(L, 78, 1) + k_brow(R, 78, 1),
                      f'<path d="M95 117 Q100 121 105 117" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'),
        "squint": face(f'<path d="M66 86 L86 96 L66 106" fill="none" stroke="{INK}" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/>'
                       f'<path d="M134 86 L114 96 L134 106" fill="none" stroke="{INK}" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/>',
                       "", k_open_mouth(117, 9, 10)),
    }


def kitsune_template():
    ear_l = f'''<g class="bd-ear bd-ear-l"><path d="{K_EAR_OUT}" class="c-fur" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/><path d="{K_EAR_IN}" class="c-warm"/></g>'''
    ear_r = f'''<g class="bd-ear bd-ear-r"><path d="{mirror(K_EAR_OUT)}" class="c-fur" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/><path d="{mirror(K_EAR_IN)}" class="c-warm"/></g>'''
    body = f"""    <g {NS} class="bd-buddy bd-kitsune" transform="translate(0 -8) translate(100 92) scale(.86) translate(-100 -92)">
      <defs>
        <linearGradient {_id("fur")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--bd-fur-lt)"/><stop offset="1" style="stop-color: var(--bd-fur)"/>
        </linearGradient>
        <linearGradient {_id("iris")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--bd-warm-dk)"/><stop offset="1" style="stop-color: var(--bd-warm)"/>
        </linearGradient>
        <clipPath {_id("tail")}><path d="{K_TAIL}"/></clipPath>
      </defs>
      {status_orb(30, 46)}
      <g class="bd-tail">
        <path d="{K_TAIL}" class="c-fur" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>
        <path d="{K_TAIL_TIP}" class="c-cream" {_url("clip-path", "tail")}/>
        <path d="M122 112 Q150 118 170 100" fill="none" class="s-fur-dk" stroke-width="2.4" stroke-linecap="round" opacity=".55"/>
        <path d="{K_TAIL}" fill="none" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>
      </g>
      <g class="bd-char">
        {ear_l}
        {ear_r}
        <t t-if="!props.head">
          <ellipse cx="100" cy="216" rx="30" ry="5" fill="#000" opacity=".2"/>
          <rect x="80" y="190" width="16" height="22" rx="8" class="c-fur-dk" stroke="{INK}" stroke-width="2.6"/>
          <rect x="104" y="190" width="16" height="22" rx="8" class="c-fur-dk" stroke="{INK}" stroke-width="2.6"/>
          <rect x="122" y="138" width="14" height="36" rx="7" transform="rotate(-8 129 142)" class="c-fur" stroke="{INK}" stroke-width="2.6"/>
          <path d="M80 130 Q100 124 120 130 L126 188 Q100 200 74 188 Z" class="c-fur" stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>
          <ellipse cx="100" cy="162" rx="14" ry="22" class="c-cream"/>
          <g class="bd-arm" t-att-class="{{'is-waving': props.waving}}" style="transform-origin: 80px 146px">
            <path d="M80 148 Q56 144 48 118" fill="none" stroke="{INK}" stroke-width="19" stroke-linecap="round"/>
            <path d="M80 148 Q56 144 48 118" fill="none" class="s-fur" stroke-width="13" stroke-linecap="round"/>
            <circle cx="47" cy="113" r="10.5" class="c-fur-dk" stroke="{INK}" stroke-width="2.4"/>
          </g>
          <path d="M72 132 Q100 148 128 132 L128 142 Q100 158 72 142 Z" class="c-warm" stroke="{INK}" stroke-width="2.6" stroke-linejoin="round"/>
        </t>
        <path d="{K_HEAD}" {_url("fill", "fur")} stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>
        <ellipse cx="100" cy="119" rx="29" ry="16" class="c-cream" opacity=".85"/>
        <path d="M100 58 C96 64 96 70 100 74 C104 70 104 64 100 58 Z" class="c-warm"/>
        <path d="M86 62 C84 66 85 70 88 72 C90 68 89 65 86 62 Z M114 62 C116 66 115 70 112 72 C110 68 111 65 114 62 Z" class="c-warm" opacity=".8"/>
        <path d="M48 102 L61 104 M47 109 L61 109 M48 116 L61 114 M152 102 L139 104 M153 109 L139 109 M152 116 L139 114" fill="none" class="s-warm-dk" stroke-width="2.4" stroke-linecap="round" opacity=".85"/>
        {K_BLUSH}
{chain(kitsune_faces())}
        <t t-call="buddy_ia.AvatarAccessories"/>
      </g>
      <t t-call="buddy_ia.AvatarFx"/>
    </g>"""
    return template("buddy_ia.Avatar.kitsune", body)


# ================================================================================================ Mochi
M_BLOB = ("M100 36 C140 36 163 60 163 94 C163 122 146 136 100 136 C54 136 37 122 37 94 C37 60 60 36 100 36 Z")
M_LEAF = "M100 40 C93 26 103 10 126 11 C127 28 117 40 100 40 Z"


def m_eye(cx, cy, look=(0, 0), rx=5.6, ry=7.6, hi="dot"):
    x, y = cx + look[0], cy + look[1]
    glint = star(x + 1.4, y - 2.8, 3.4, "#fff") if hi == "star" else f'<circle cx="{x + 1.5}" cy="{y - 2.6}" r="2.5" fill="#fff"/>'
    wet = f'<ellipse cx="{x}" cy="{y + 3.8}" rx="3.4" ry="1.6" fill="#fff" opacity=".5"/>' if hi == "wet" else ""
    return (f'<g class="bd-eye"><ellipse cx="{x:g}" cy="{y:g}" rx="{rx}" ry="{ry}" fill="{INK}"/>{glint}'
            f'<circle cx="{x - 1.6:g}" cy="{y + 3.2:g}" r="1.1" fill="#fff" opacity=".85"/>{wet}</g>')


def m_arc(cx, cy, w=7, up=True, sw=3.6):
    c = cy - 10 if up else cy + 6
    return (f'<path d="M{cx - w} {cy + (2 if up else -3)} Q{cx} {c} {cx + w} {cy + (2 if up else -3)}" fill="none" '
            f'stroke="{INK}" stroke-width="{sw}" stroke-linecap="round"/>')


def m_brow(cx, y, tilt=0, w=5.5):
    return (f'<path d="M{cx - w} {y + tilt} L{cx + w} {y - tilt}" fill="none" stroke="{INK}" stroke-width="2.6" '
            f'stroke-linecap="round" opacity=".8"/>')


M_W = f'<path d="M93.5 105 Q96.7 109.5 100 105.5 Q103.3 109.5 106.5 105" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>'


def m_open(cy=106, rx=6.5, ry=8, tongue=True):
    t = f'<ellipse cx="100" cy="{cy + ry * 0.6:g}" rx="{rx * 0.6:g}" ry="{ry * 0.36:g}" fill="#ff8aa8"/>' if tongue else ""
    return (f'<path d="M{100 - rx} {cy - ry * 0.35:g} Q100 {cy + ry * 1.35:g} {100 + rx} {cy - ry * 0.35:g} Z" fill="#5a1730" '
            f'stroke="{INK}" stroke-width="2.2" stroke-linejoin="round"/>' + t)


def mochi_faces():
    L, R, Y = 76, 124, 90

    def face(eyes, brows, mouth, extra=""):
        return (f'<g class="bd-look"><g class="bd-eyes">{eyes}</g><g class="bd-brows">{brows}</g></g>'
                f'<g class="bd-mouth">{mouth}</g>{extra}')

    pair = lambda **kw: m_eye(L, Y, **kw) + m_eye(R, Y, **kw)
    wavy = f'<path d="M92 107 Q96 102 100 107 T108 107" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'
    more_blush = '<ellipse cx="62" cy="101" rx="11" ry="6.6" class="c-blush" opacity=".35"/><ellipse cx="138" cy="101" rx="11" ry="6.6" class="c-blush" opacity=".35"/>'
    return {
        "neutral": face(pair(), "", M_W),
        "happy": face(m_arc(L, Y) + m_arc(R, Y), "", m_open(105, 6, 7), more_blush),
        "thinking": face(pair(look=(2.4, -2.8)), m_brow(L, 76, 0) + m_brow(R, 72, -2), f'<path d="M96 107 Q100 104.6 104 107" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'),
        "surprised": face(pair(rx=6.4, ry=9.2), m_brow(L, 71) + m_brow(R, 71), m_open(109, 3.6, 5, False)),
        "doubt": face(m_eye(L, Y) + f'<g class="bd-eye"><path d="M118.4 90 A5.6 7.6 0 0 0 129.6 90 Z" fill="{INK}"/>'
                      f'<path d="M116.4 90 L131.6 90" stroke="{INK}" stroke-width="3" stroke-linecap="round"/><circle cx="123" cy="92.8" r="1.6" fill="#fff"/></g>',
                      m_brow(L, 74, 2) + m_brow(R, 80, -1), wavy),
        "wink": face(m_arc(L, Y) + m_eye(R, Y), "", M_W),
        "sparkle": face(pair(rx=6.4, ry=8.6, hi="star"), "", m_open(105, 6, 7), more_blush),
        "heart": face(heart(L, Y, 1.5, "bd-heart", "#ff5c8a", INK, 2) + heart(R, Y, 1.5, "bd-heart", "#ff5c8a", INK, 2), "", M_W, more_blush),
        "tears": face(pair(hi="wet"), m_brow(L, 78, -2) + m_brow(R, 78, 2),
                      f'<path d="M94 108 Q100 102.5 106 108" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>',
                      '<path class="bd-tear" d="M70 98 Q65 110 69 118 Q77 110 70 98Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.6"/>'
                      '<path class="bd-tear" d="M130 98 Q135 110 131 118 Q123 110 130 98Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.6"/>'),
        "dizzy": face(spiral(L, Y, 8) + spiral(R, Y, 8), "", wavy),
        "mad": face(f'<g class="bd-eye"><path d="M70.4 91 A5.6 6.6 0 0 0 81.6 91 L81 85 L71 82 Z" fill="{INK}"/><circle cx="74" cy="90" r="1.7" fill="#fff"/></g>'
                    f'<g class="bd-eye"><path d="M118.4 91 A5.6 6.6 0 0 0 129.6 91 L129 82 L119 85 Z" fill="{INK}"/><circle cx="122" cy="90" r="1.7" fill="#fff"/></g>',
                    f'<path d="M66 77 L84 84" fill="none" stroke="{INK}" stroke-width="3.2" stroke-linecap="round"/>'
                    f'<path d="M134 77 L116 84" fill="none" stroke="{INK}" stroke-width="3.2" stroke-linecap="round"/>',
                    f'<path d="M94 108 Q100 102 106 108" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>'),
        "yawn": face(m_arc(L, Y + 2, up=False) + m_arc(R, Y + 2, up=False), "", m_open(108, 7.4, 9.4),
                     '<path class="bd-tear" d="M62 88 Q59.5 93 62 96.5 Q64.5 93 62 88Z" fill="#aee6ff" stroke="#4aa3d6" stroke-width="1.3"/>'),
        "sleep": face(m_arc(L, Y + 2, up=False) + m_arc(R, Y + 2, up=False), "",
                      f'<path d="M96 106 Q100 109.5 104 106" fill="none" stroke="{INK}" stroke-width="2.4" stroke-linecap="round"/>'),
        "squint": face(f'<path d="M69 82 L84 90 L69 98" fill="none" stroke="{INK}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
                       f'<path d="M131 82 L116 90 L131 98" fill="none" stroke="{INK}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
                       "", m_open(105, 7, 7.6)),
    }


def mochi_template():
    body = f"""    <g {NS} class="bd-buddy bd-mochi" transform="translate(0 -2) translate(100 86) scale(.9) translate(-100 -86)">
      <defs>
        <linearGradient {_id("mochi")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color: var(--bd-mochi)"/><stop offset=".62" style="stop-color: var(--bd-mochi)"/><stop offset="1" style="stop-color: var(--bd-mochi-dk)"/>
        </linearGradient>
        <linearGradient {_id("leaf")} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" style="stop-color: var(--bd-leaf-dk)"/><stop offset="1" style="stop-color: var(--bd-leaf)"/>
        </linearGradient>
      </defs>
      {status_orb(36, 40)}
      <g class="bd-char">
        <t t-if="!props.head">
          <ellipse cx="100" cy="146" rx="46" ry="6" fill="#000" opacity=".2"/>
          <ellipse cx="78" cy="136" rx="13" ry="7.5" class="c-mochi-dk" stroke="{INK}" stroke-width="2.6"/>
          <ellipse cx="122" cy="136" rx="13" ry="7.5" class="c-mochi-dk" stroke="{INK}" stroke-width="2.6"/>
          <ellipse cx="160" cy="108" rx="9" ry="6.5" transform="rotate(30 160 108)" class="c-mochi" stroke="{INK}" stroke-width="2.6"/>
          <g class="bd-arm" t-att-class="{{'is-waving': props.waving}}" style="transform-origin: 42px 108px">
            <ellipse cx="38" cy="100" rx="10" ry="6.8" transform="rotate(-40 38 100)" class="c-mochi" stroke="{INK}" stroke-width="2.6"/>
          </g>
        </t>
        <path d="{M_BLOB}" {_url("fill", "mochi")} stroke="{INK}" stroke-width="3" stroke-linejoin="round"/>
        <path d="M58 56 Q74 42 96 42" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" opacity=".75"/>
        <path d="M50 70 Q52 64 56 60" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" opacity=".6"/>
        <ellipse cx="62" cy="101" rx="10.5" ry="6.4" class="c-blush"/><ellipse cx="138" cy="101" rx="10.5" ry="6.4" class="c-blush"/>
        <g class="bd-leaf">
          <path d="{M_LEAF}" {_url("fill", "leaf")} stroke="{INK}" stroke-width="2.8" stroke-linejoin="round"/>
          <path d="M101 38 Q108 26 122 14" fill="none" stroke="{INK}" stroke-width="1.8" stroke-linecap="round" opacity=".55"/>
        </g>
{chain(mochi_faces())}
        <t t-call="buddy_ia.AvatarAccessories"/>
      </g>
      <t t-call="buddy_ia.AvatarFx"/>
    </g>"""
    return template("buddy_ia.Avatar.mochi", body)


# ================================================================================================ Accesorios
# Dibujados en un espacio canónico (cabeza de radio ~50, ojos separados 48) y colocados por el registro de JS
# (buddy_registry.js: `acc`) con translate/rotate/scale: así calzan en los tres buddys sin repetir el dibujo.
def accessories_template():
    s = f'stroke="{INK}" stroke-width="2.6" stroke-linejoin="round"'
    body = f"""    <g {NS} class="bd-accs">
      <t t-foreach="accessoryList" t-as="acc" t-key="acc.id">
        <g t-att-transform="acc.transform">
          <g t-att-class="acc.className">
            <t t-if="acc.id === 'mustache'">
              <path d="M0 -3 C-8 -10 -21 -9 -26 -1 C-28 4 -22 8 -18 5.5 C-21 12 -9 13 -4 6 Q0 3.6 4 6 C9 13 21 12 18 5.5 C22 8 28 4 26 -1 C21 -9 8 -10 0 -3Z" fill="#7b4b2c" {s}/>
              <path d="M-4 -1.6 C-10 -5.4 -18 -4.4 -21 0.6 M4 -1.6 C10 -5.4 18 -4.4 21 0.6" fill="none" stroke="#8a5a3a" stroke-width="1.6" stroke-linecap="round"/>
            </t>
            <t t-elif="acc.id === 'wizard'">
              <path d="M-30 3 C-28 -24 -8 -52 18 -68 C28 -72 36 -64 31 -57 C25 -52 22 -26 30 3Z" fill="#6a4bd8" {s}/>
              <path d="M30 3 C22 -26 25 -52 31 -57 C36 -64 28 -72 18 -68 C22 -50 14 -22 8 3Z" fill="#4d34ad" opacity=".55"/>
              <path d="M-31 -3 Q0 8 31 -3 L30 -12 Q0 -1 -30 -12Z" fill="#ffd84a" {s}/>
              {star(-2, -32, 6.5, "#ffe27a")}
              <circle cx="11" cy="-44" r="2.2" fill="#ffe27a"/><circle cx="-14" cy="-16" r="1.8" fill="#ffe27a"/>
              <ellipse cx="0" cy="3" rx="47" ry="10" fill="#5b3fc4" {s}/>
              <path d="M-36 2 Q0 12 36 2" fill="none" stroke="#8c72ff" stroke-width="2" stroke-linecap="round" opacity=".7"/>
            </t>
            <t t-elif="acc.id === 'party'">
              <path d="M-19 4 L0 -52 L19 4 Q0 11 -19 4Z" fill="#ff6fae" {s}/>
              <path d="M-14 -9 Q0 -2 14 -9 L11 -17 Q0 -11 -11 -17Z M-8 -27 Q0 -22 8 -27 L5 -35 Q0 -31 -5 -35Z" fill="#ffe27a"/>
              <path d="M-19 4 L0 -52 L19 4 Q0 11 -19 4Z" fill="none" {s}/>
              <circle cx="0" cy="-54" r="6.4" fill="#7af0ff" {s}/>
            </t>
            <t t-elif="acc.id === 'shades'">
              <path d="M-40 -4 L-52 -8 M40 -4 L52 -8" fill="none" stroke="#0a0a1a" stroke-width="3.4" stroke-linecap="round"/>
              <path d="M-10 -3 Q0 -9 10 -3" fill="none" stroke="#0a0a1a" stroke-width="3.6" stroke-linecap="round"/>
              <path d="M-39 -8 L-9 -8 Q-9 12 -22 12 Q-39 12 -39 -2Z" fill="#1b2150" stroke="#a9b6ff" stroke-width="2.4" stroke-linejoin="round"/>
              <path d="M39 -8 L9 -8 Q9 12 22 12 Q39 12 39 -2Z" fill="#1b2150" stroke="#a9b6ff" stroke-width="2.4" stroke-linejoin="round"/>
              <path d="M-34 -3 L-24 -3 M-30 3 L-26 3 M34 -3 L24 -3 M30 3 L26 3" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" opacity=".55"/>
              <path d="M-39 -8 L-9 -8 M39 -8 L9 -8" fill="none" stroke="#fff" stroke-width="1.4" opacity=".35"/>
            </t>
            <t t-elif="acc.id === 'headphones'">
              <path d="M-49 8 C-53 -62 53 -62 49 8" fill="none" stroke="{INK}" stroke-width="12" stroke-linecap="round"/>
              <path d="M-49 8 C-53 -62 53 -62 49 8" fill="none" class="s-primary" stroke-width="7" stroke-linecap="round"/>
              <rect x="-62" y="-12" width="20" height="36" rx="9" class="c-primary" {s}/>
              <rect x="42" y="-12" width="20" height="36" rx="9" class="c-primary" {s}/>
              <rect x="-57" y="-5" width="10" height="22" rx="5" fill="#0b0f2b" opacity=".85"/>
              <rect x="47" y="-5" width="10" height="22" rx="5" fill="#0b0f2b" opacity=".85"/>
            </t>
          </g>
        </g>
      </t>
    </g>"""
    return template("buddy_ia.AvatarAccessories", body)


# ================================================================================================ Efectos de gestos
def glyph(d, fill="#fff", ol=INK, olw=7.5, w=4.2):
    """Trazo con contorno oscuro: se lee sobre fondo claro y oscuro."""
    return (f'<path d="{d}" fill="none" stroke="{ol}" stroke-width="{olw}" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"/>')


def fx_template():
    A = lambda name: f't-att-transform="anchor(\'{name}\')"'
    zz = lambda x, y, k: glyph(f"M{x - 5 * k:g} {y - 5 * k:g} H{x + 5 * k:g} L{x - 5 * k:g} {y + 5 * k:g} H{x + 5 * k:g}", "#fff", INK, 7, 3.6)
    body = f"""    <g {NS} class="bd-fxlayer">
      <g t-if="fxs.includes('sweat')" {A("temple")}><g class="bd-fx bd-fx-sweat">
        <path d="M0 -10 C6 -2 8 3 8 6 A8 8 0 1 1 -8 6 C-8 3 -6 -2 0 -10Z" fill="#9adcff" stroke="{INK}" stroke-width="2.4" stroke-linejoin="round"/>
        <path d="M-3.4 3 Q-4.6 6.6 -2.2 9" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
      </g></g>
      <g t-if="fxs.includes('vein')" {A("forehead")}><g class="bd-fx bd-fx-vein">
        {glyph("M-9 -3 Q-3 -3 -3 -9 M9 -3 Q3 -3 3 -9 M-9 3 Q-3 3 -3 9 M9 3 Q3 3 3 9", "#ff4d4d", INK, 8.2, 4.4)}
      </g></g>
      <g t-if="fxs.includes('sparkles')" {A("top")}><g class="bd-fx bd-fx-sparkles">
        <g class="bd-spark s1" transform="translate(-52 6)">{star(0, 0, 12, "#fff3a8").replace("/>", f' stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')}</g>
        <g class="bd-spark s2" transform="translate(54 -2)">{star(0, 0, 14, "#fff3a8").replace("/>", f' stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')}</g>
        <g class="bd-spark s3" transform="translate(-30 -18)">{star(0, 0, 8.5, "#fff").replace("/>", f' stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/>')}</g>
        <g class="bd-spark s4" transform="translate(34 28)">{star(0, 0, 8.5, "#fff").replace("/>", f' stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/>')}</g>
      </g></g>
      <g t-if="fxs.includes('hearts')" {A("top")}><g class="bd-fx bd-fx-hearts">
        <g class="bd-float f1" transform="translate(-26 6)">{heart(0, 0, 1.5)}</g>
        <g class="bd-float f2" transform="translate(30 -4)">{heart(0, 0, 1.1)}</g>
        <g class="bd-float f3" transform="translate(6 -14)">{heart(0, 0, 0.8)}</g>
      </g></g>
      <g t-if="fxs.includes('question')" {A("top")}><g class="bd-fx bd-fx-question">
        {glyph("M-7 -9 C-7 -19 9 -19 9 -9 C9 -2 0 -3 0 5", "#fff", INK, 8.4, 4.6)}
        <circle cx="0" cy="12.5" r="2.9" fill="#fff" stroke="{INK}" stroke-width="2.6"/>
      </g></g>
      <g t-if="fxs.includes('exclaim')" {A("top")}><g class="bd-fx bd-fx-exclaim">
        {glyph("M0 -17 L0 1", "#fff", INK, 9, 5.2)}
        <circle cx="0" cy="10.5" r="3.3" fill="#fff" stroke="{INK}" stroke-width="2.6"/>
      </g></g>
      <g t-if="fxs.includes('zzz')" {A("zzz")}><g class="bd-fx bd-fx-zzz">
        <g class="bd-z z1">{zz(0, 0, 1)}</g><g class="bd-z z2">{zz(9, -13, 1.35)}</g><g class="bd-z z3">{zz(21, -30, 1.8)}</g>
      </g></g>
      <g t-if="fxs.includes('bubble')" {A("nose")}><g class="bd-fx bd-fx-bubble">
        <circle cx="0" cy="0" r="8" fill="#bfeaff" fill-opacity=".6" stroke="#58b5e6" stroke-width="2"/>
        <path d="M-4.4 -2.6 Q-3.4 -5 -0.6 -5.4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
      </g></g>
      <g t-if="fxs.includes('stars')" {A("top")}><g class="bd-fx bd-fx-stars">
        <g class="bd-orbit">
          <g transform="translate(-30 0)">{star(0, 0, 7, "#ffe27a").replace("/>", f' stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')}</g>
          <g transform="translate(30 0)">{star(0, 0, 7, "#ffe27a").replace("/>", f' stroke="{INK}" stroke-width="1.6" stroke-linejoin="round"/>')}</g>
          <g transform="translate(0 -8)">{star(0, 0, 6, "#fff").replace("/>", f' stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/>')}</g>
        </g>
      </g></g>
    </g>"""
    return template("buddy_ia.AvatarFx", body)


def avatar_template():
    body = """    <svg class="o_buddy_avatar" t-att-class="rootClass" t-att-viewBox="viewBox" t-att-width="svgWidth" t-att-height="svgHeight" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient t-attf-id="bd-rainbow-{{uid}}" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#34e8ff"/><stop offset=".3" stop-color="#7c5cff"/>
          <stop offset=".55" stop-color="#ff4fd8"/><stop offset=".78" stop-color="#ffd84a"/><stop offset="1" stop-color="#46f0a0"/>
        </linearGradient>
      </defs>
      <t t-call="{{buddyTemplate}}"/>
    </svg>"""
    return template("buddy_ia.Avatar", body)


def write_templates():
    note = "una sola fuente para los dos buddys"
    files = {
        "buddy_avatar.xml": [avatar_template(), accessories_template(), fx_template()],
        "buddy_avatar_kitsune.xml": [kitsune_template()],
        "buddy_avatar_mochi.xml": [mochi_template()],
    }
    for name, templates in files.items():
        (XML_DIR / name).write_text(wrap(templates, note), encoding="utf-8", newline="\n")
    return list(files)


if __name__ == "__main__":
    print("OK:", ", ".join(write_templates()))
