s = """sk-C6xqrLS9PLxwJoURjdlvQm6fJcYGz3z4IYMoJdXfjF3Rxbk2"""
bad = [(i, ch, hex(ord(ch))) for i, ch in enumerate(s) if ord(ch) > 255]
print("len=", len(s))
print("bad=", bad[:20])
print("repr=", repr(s))
