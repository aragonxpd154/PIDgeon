"""Árvore genérica de campos usada para exibir tabelas e descritores."""


class Node:
    __slots__ = ("label", "value", "children")

    def __init__(self, label, value=None, children=None):
        self.label = label
        self.value = value
        self.children = children if children is not None else []

    def add(self, label, value=None, children=None):
        n = Node(label, value, children)
        self.children.append(n)
        return n

    def extend(self, nodes):
        self.children.extend(nodes)
        return self

    def text(self):
        if self.value is None or self.value == "":
            return str(self.label)
        return "%s: %s" % (self.label, self.value)

    def to_dict(self):
        d = {"label": self.label}
        if self.value is not None:
            d["value"] = self.value if isinstance(self.value, (int, float, str, bool)) else str(self.value)
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d

    def dump(self, indent=0):
        lines = ["  " * indent + self.text()]
        for c in self.children:
            lines.append(c.dump(indent + 1))
        return "\n".join(lines)

    def __repr__(self):
        return "Node(%r)" % self.text()
