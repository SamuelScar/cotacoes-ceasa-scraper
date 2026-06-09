# Estrategias contra bloqueio

O scraper reduz requisicoes desnecessarias e interrompe a coleta quando uma
fonte recusa o acesso. Ele nao tenta contornar bloqueios.

## Comportamento implementado

- Intervalo minimo configurado por `COTACOES_REQUEST_DELAY_SECONDS`.
- Jitter de ate 0,5 segundo entre requisicoes.
- Cache limitado de respostas GET e POST durante a execucao.
- Sessao e cookies preservados durante a execucao.
- Ate quatro tentativas com backoff para falhas temporarias.
- Respeito ao cabecalho `Retry-After`.
- Interrupcao imediata em HTTP 403.
- Interrupcao em HTTP 429 persistente.
- Interrupcao da fonte em falhas HTTP ou de conexao persistentes.

O cache em memoria nao continua entre execucoes. Para reutilizar raws ativos,
configure:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```

## Coletas longas

Baixe primeiro e processe depois quando quiser preservar o progresso:

```bash
docker compose run --rm baixar
docker compose run --rm salvar
```

Cada raw encontrado e salvo durante o download. Se uma fonte falhar, as demais
continuam no fluxo de todas as fontes.

Se `tudo` for interrompido antes da fase de persistencia, execute `salvar`
depois para processar os raws preservados.

Com `COTACOES_INCREMENTAL_HISTORY=true`, uma nova execucao historica continua
antes do raw ativo mais antigo e evita solicitar novamente toda a janela mais
recente. `COTACOES_REUSE_RAW_BEFORE_REQUEST` continua reservado para reutilizar
datas que forem solicitadas novamente.

## Praticas nao adotadas

O projeto nao usa rotacao de IP, proxies, troca de fingerprint, rotacao de
User-Agent ou limpeza de cookies para contornar recusas da fonte. Ao receber um
bloqueio, reduza a janela ou o ritmo e retome depois.
