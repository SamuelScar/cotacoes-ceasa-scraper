# Estrategias para reduzir risco de bloqueio

Nao existe garantia de que uma fonte publica nunca bloqueara o scraper. O
objetivo do projeto e reduzir requisicoes desnecessarias, respeitar limites
informados pelos servidores e interromper a coleta quando a fonte recusar o
acesso.

## Estrategias implementadas

### Intervalo conservador com jitter

O `HttpClient` mantem o intervalo minimo configurado por
`COTACOES_REQUEST_DELAY_SECONDS` e adiciona uma pequena variacao de ate 0,5
segundo. A variacao evita rajadas sincronizadas sem tentar simular navegacao
humana.

### Cache durante a execucao

Respostas GET e POST bem-sucedidas ficam em um cache limitado durante a
execucao atual.

Esse cache evita baixar novamente:

- paginas de indice consultadas para varias datas;
- formularios usados para descobrir categorias e datas;
- relatorios baixados durante a descoberta de datas e solicitados novamente
  durante o salvamento do raw.

O cache nao e persistido entre execucoes. Para reutilizar arquivos ja salvos
em `data/raw/`, use `COTACOES_REUSE_RAW_BEFORE_REQUEST=true`.

### Backoff para falhas temporarias

O cliente faz ate quatro tentativas para:

- `408 Request Timeout`;
- `429 Too Many Requests`;
- `500 Internal Server Error`;
- `502 Bad Gateway`;
- `503 Service Unavailable`;
- `504 Gateway Timeout`;
- falhas temporarias de conexao.

O intervalo entre tentativas cresce exponencialmente e recebe um pequeno
jitter. Quando a resposta inclui `Retry-After`, o tempo informado pela fonte
e respeitado.

### Interrupcao em bloqueio

- `403 Forbidden` interrompe imediatamente a coleta da fonte.
- `429 Too Many Requests` interrompe a coleta da fonte quando continua
  ocorrendo apos as tentativas com backoff.

Esses erros nao sao ignorados pelo fluxo que descobre datas ou pelo fluxo que
continua apos falhas pontuais de categoria.

### Sessao preservada

O `CookieJar` e mantido durante a execucao. Isso e necessario para fontes com
formularios dependentes de sessao, como a CEASA-ES.

## Estrategias nao adotadas

O projeto nao deve implementar:

- rotacao de User-Agent para se passar por navegadores diferentes;
- rotacao de IP ou proxies para contornar bloqueios;
- alteracao de fingerprint TLS para esconder o cliente;
- limpeza de cookies para aparentar novas sessoes;
- reinicio automatico do roteador.

Essas medidas tentam contornar recusas da fonte, aumentam a complexidade e nao
reduzem a carga gerada pelo scraper.

## Fluxo recomendado para coletas longas

Para coletar 100 datas, configure `COTACOES_QUOTES_BACK=99`, pois a data
limite tambem entra na contagem:

```env
COTACOES_QUOTES_BACK=99
```

No modo de download, cada data encontrada e salva imediatamente. Assim, uma
interrupcao posterior nao remove os raws que ja foram obtidos.

Para baixar e processar todas as fontes:

```bash
docker compose run --rm tudo
```

Para retomar uma coleta interrompida sem baixar novamente os raws ativos,
ative temporariamente:

```env
COTACOES_REUSE_RAW_BEFORE_REQUEST=true
```

Fontes que nao suportam historico por data, como CEASA-MG, CEASA-CE e
CEASA-DF, fornecem apenas a cotacao atual disponivel. A CEAGESP-SP suporta
datas anteriores, mas a pagina oficial expoe apenas uma janela recente que
pode conter menos de 100 cotacoes.

## Prioridades futuras

- Avaliar cache condicional com `ETag` e `Last-Modified` nas fontes que
  retornarem esses cabecalhos.
- Registrar quantidade de requisicoes e erros por fonte.
- Consultar e documentar `robots.txt` e termos de uso de cada fonte.
