"""Bootstrap test suite — memastikan test hermetik.

Cache persisten (L2 Supabase) global dinonaktifkan agar test tidak melakukan
network call nyata dan tidak bergantung pada isi .env developer (mis. key
Supabase yang baru diisi akan membuat test memanggil Supabase sungguhan dan
gagal flaky karena race background thread). Test yang ingin menguji lapisan
L2 menyuntikkan fake sendiri (mis. tests/test_cache_supabase.py,
tests/test_director_persistent_cache.py).
"""

import data.cache as _cache_mod

_cache_mod.persistent.enabled = False
