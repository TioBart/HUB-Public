"""
Gera o arquivo cifrado de uma carteira para o Hub Econômico.

Uso:
    python cifrar_carteira.py CODIGO carteira.json [cotacoes.json]

Cria data/carteiras/<id>.json, onde o id é derivado do código. Só quem souber o código
consegue abrir o conteúdo no site. Os arquivos carteira.json e cotacoes.json originais
NÃO devem ir para o repositório (eles estão no .gitignore).

Para trocar o código de uma carteira: gere o arquivo com o código novo e apague o antigo
(o script mostra o nome do arquivo de cada código).

Requer: pip install cryptography
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SAL_ID = b"hub-economico:carteira-id:v1"
ITER_ID = 200_000
ITER_CHAVE = 600_000
PASTA = Path(__file__).resolve().parent / "data" / "carteiras"


def id_carteira(codigo: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", codigo.encode(), SAL_ID, ITER_ID, dklen=16).hex()


def cifrar(codigo: str, pacote: dict) -> dict:
    sal, iv = os.urandom(16), os.urandom(12)
    chave = hashlib.pbkdf2_hmac("sha256", codigo.encode(), sal, ITER_CHAVE, dklen=32)
    dados = AESGCM(chave).encrypt(iv, json.dumps(pacote, ensure_ascii=False, separators=(",", ":")).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER_CHAVE, "salt": b64(sal), "iv": b64(iv), "dados": b64(dados)}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    codigo, arq_carteira = sys.argv[1], sys.argv[2]
    carteira = json.loads(Path(arq_carteira).read_text(encoding="utf-8"))
    if "atualizacoes" not in carteira:
        sys.exit("carteira.json inválido: falta a chave 'atualizacoes'.")
    pacote = {"carteira": carteira}
    if len(sys.argv) > 3:
        pacote["cotacoes"] = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    PASTA.mkdir(parents=True, exist_ok=True)
    destino = PASTA / f"{id_carteira(codigo)}.json"
    destino.write_text(json.dumps(cifrar(codigo, pacote)), encoding="utf-8")
    print(f"Carteira cifrada em {destino.relative_to(Path(__file__).resolve().parent)}")


if __name__ == "__main__":
    main()
