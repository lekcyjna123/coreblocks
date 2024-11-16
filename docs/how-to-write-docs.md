https://www.sphinx-doc.org/en/master/usage/restructuredtext/directives.html#toctree-directive



Referencing a symbol:
Reference has three parts:
`<domain>:<directive>:<mod><name>`
If the used `<domain> is default (as in our case, where `py` domain is used) it can be skiped, so there is:
`<directive>:<mod><name>`

`<directive>` is a command given during an entry creation, in pur cases it is usualy the type of object, so we have:
- func
- class
- meth
- type
Full list can be found here: https://www.sphinx-doc.org/en/master/usage/domains/python.html#cross-referencing-python-objects

Modificator before the name change the behaviour of the 
