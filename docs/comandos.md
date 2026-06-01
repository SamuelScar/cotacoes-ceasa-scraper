# Comandos

Comandos principais para preparar o ambiente e executar o scraper.

## Preparar ambiente

Criar ambiente virtual:

```bash
python3 -m venv .venv
```

Ativar ambiente:

```bash
source .venv/bin/activate
```

Instalar dependencias:

```bash
pip install -r requirements.txt
```

Criar `.env` local:

```bash
cp .env.example .env
```

## Verificar CLI

Mostra os parametros disponiveis sem acessar a internet:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --help
```

Teste controlado com uma categoria:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --parse
```

Resultado esperado:

```text
aves-e-ovos: 9 cotacoes
total: 9 cotacoes extraidas.
```

## Listar categorias da CEASA-PE

Faz uma requisicao para a URL base e lista as categorias encontradas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --list-categories
```

## Baixar HTML bruto

Baixar a categoria configurada no `.env`:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main
```

Baixar uma categoria especifica:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos
```

Baixar todas as categorias descobertas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas
```

Os arquivos sao salvos em `data/raw/<fonte>/`. Para cada fonte, categoria, data de cotacao consultada e dia de execucao, a pasta principal fica somente com o HTML mais recente. Arquivos anteriores do mesmo grupo no mesmo dia sao movidos para `data/raw/<fonte>/old/`.

## Baixar e extrair cotacoes

Extrair a categoria configurada no `.env`:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --parse
```

Extrair uma categoria especifica:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --parse
```

Extrair todas as categorias descobertas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --parse
```

Resultado observado em 2026-06-01:

```text
total: 326 cotacoes extraidas.
```

## Salvar no SQLite

Baixar, extrair e salvar uma categoria especifica:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --save
```

Baixar, extrair e salvar todas as categorias descobertas:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --save
```

Baixar, extrair e salvar uma data especifica:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --target-date 29/05/2026 --save
```

Resultado observado para 29/05/2026:

```text
datas: 2026-05-29
aves-e-ovos: 9 cotacoes
carnes-e-laticinios: 15 cotacoes
cereais-e-diversos: 16 cotacoes
flores 2026-05-29: erro - Tabela de cotacoes da CEASA-PE nao encontrada.
flores: 0 cotacoes
frutas: 83 cotacoes
hortalicas: 81 cotacoes
organicos 2026-05-29: erro - Tabela de cotacoes da CEASA-PE nao encontrada.
organicos: 0 cotacoes
pescados: 12 cotacoes
total: 216 cotacoes extraidas. 216 registros novos salvos em data/cotacoes.sqlite.
```

Baixar, extrair e salvar datas de cotacao anteriores:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --quotes-back 30 --save
```

Com `--quotes-back 30`, o scraper coleta a data alvo e mais 30 datas anteriores que realmente tenham cotacao. Dias sem cotacao sao ignorados.

Se `--target-date` nao for informado e `COTACOES_TARGET_DATE` estiver vazio, a data alvo sera a data atual.

Exemplo usando a data atual como ponto de partida:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --quotes-back 30 --save
```

Exemplo usando uma data especifica como ponto de partida:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --target-date 29/05/2026 --quotes-back 30 --save
```

Nesse caso, o scraper coleta `29/05/2026` e mais 30 datas de cotacao anteriores encontradas. Ele nao coleta de `29/05/2026` ate hoje.

Quando uma categoria nao tem tabela para uma data, o erro e exibido e as outras categorias continuam sendo processadas.

Resultado observado em 2026-06-01:

```text
total: 326 cotacoes extraidas. 317 registros novos salvos em data/cotacoes.sqlite.
```

Os 317 registros novos ocorreram porque 9 registros da categoria `aves-e-ovos` ja tinham sido salvos em um teste anterior.

O caminho padrao do banco vem do `.env`:

```env
COTACOES_DATABASE_PATH=data/cotacoes.sqlite
```

Tambem pode ser alterado por comando:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --save --database-path data/teste.sqlite
```

Sobrescrever URL base sem alterar `config/fontes.json`:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category aves-e-ovos --base-url https://www.ceasape.org.br/cotacao
```

Consultar quantidade de registros pelo Python:

```bash
python -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); print(con.execute('select count(*) from cotacoes').fetchone()[0])"
```

Consultar alguns registros:

```bash
python -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); rows=con.execute('select c.nome, ca.slug, p.nome_original, u.sigla, co.preco_comum, co.data_cotacao from cotacoes co join ceasas c on c.id = co.ceasa_id join categorias ca on ca.id = co.categoria_id join produtos p on p.id = co.produto_id left join unidades u on u.id = co.unidade_id limit 5').fetchall(); [print(row) for row in rows]"
```

Listar tabelas do banco:

```bash
python -c "import sqlite3; con=sqlite3.connect('data/cotacoes.sqlite'); print([r[0] for r in con.execute(\"select name from sqlite_master where type='table' order by name\")])"
```

## Limites de coleta

O intervalo minimo entre requisicoes vem do `.env`:

```env
COTACOES_REQUEST_DELAY_SECONDS=2.0
```

A data alvo e a janela de cotacoes tambem vem do `.env`:

```env
COTACOES_TARGET_DATE=
COTACOES_QUOTES_BACK=0
```

Com `COTACOES_TARGET_DATE=` vazio, a CLI usa a data atual. Com `COTACOES_QUOTES_BACK=0`, coleta somente essa data alvo.

Tambem pode ser alterado por comando:

```bash
PYTHONPATH=src python -m cotacoes_ceasa.main --category todas --request-delay-seconds 3
```
