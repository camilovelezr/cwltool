# Stubs for networkx.algorithms.link_prediction (Python 3.5)
#
# NOTE: This dynamically typed stub was automatically generated by stubgen.

from typing import Any, Optional

def resource_allocation_index(G, ebunch: Optional[Any] = ...): ...
def jaccard_coefficient(G, ebunch: Optional[Any] = ...): ...
def adamic_adar_index(G, ebunch: Optional[Any] = ...): ...
def preferential_attachment(G, ebunch: Optional[Any] = ...): ...
def cn_soundarajan_hopcroft(G, ebunch: Optional[Any] = ..., community: str = ...): ...
def ra_index_soundarajan_hopcroft(G, ebunch: Optional[Any] = ..., community: str = ...): ...
def within_inter_cluster(
    G, ebunch: Optional[Any] = ..., delta: float = ..., community: str = ...
): ...
