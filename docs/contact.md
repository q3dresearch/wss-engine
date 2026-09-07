# WSS_CONTACT — who is running this capture

Every request carries a User-Agent like:

```
wss/0.5.0 (contact: https://github.com/q3dresearch/wss-openrouter)
```

`WSS_CONTACT` is that contact string. Capture refuses to run without it.

## Why it exists

It is not bureaucracy, and it buys three concrete things:

1. **A publisher who has a problem with your traffic can reach you** instead
   of silently IP-banning you. That is the whole bargain: identified traffic
   gets a conversation, anonymous traffic gets a block.
2. **Some publishers require it.** SEC EDGAR refuses requests without a
   contact in the User-Agent; arXiv and several others ask for one. A source
   you want later may simply not work without this.
3. **It is the honest half of the robots.txt contract.** The engine honours
   robots.txt, waits per-host delays, and says who it is. Those three
   together are what separate a well-behaved archive from a scraper.

## It does not have to be an email

**A GitHub identity plus repo URL is the better default**, and is what the
scaffold now suggests:

```
WSS_CONTACT=<username> +https://github.com/<owner>/<repo>
```

It names exactly who is running the capture, leads to an issue tracker where
anyone can complain, and contains **no personal data**.

An email works too and is more direct, but understand what it means: the
contact string is **transmitted to every publisher you capture from** and
lands in their server logs. It is not published in the repo — it lives only
in `.env.local` and CI secrets — but it is disclosed to each publisher. If
you use an email, prefer a role address over a personal one.

What does *not* work is anything unreachable: a GitHub `noreply` address
receives no mail, and a fake value like `you@example.com` is worse than
useless — it identifies your traffic as somebody who did not bother.

## Forks set their own

The value lives in `.env.local` (gitignored) locally, and in a repository
secret in CI. **Neither travels with a fork.** Someone who forks your repo
gets `.env.example`, not your value, and capture refuses to run until they
set their own.

That is deliberate: it is their traffic, hitting publishers under their name,
and they should make that choice consciously rather than inherit yours. It
also means your contact details can never leak through a fork.
