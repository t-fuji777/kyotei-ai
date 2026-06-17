export default {
  async scheduled(event, env, ctx) {
    const url = "https://api.github.com/repos/t-fuji777/kyotei-ai/actions/workflows/daily.yml/dispatches";
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + env.GH_PAT,
        "Accept": "application/vnd.github+json",
        "User-Agent": "kyotei-ai-trigger",
        "X-GitHub-Api-Version": "2022-11-28"
      },
      body: JSON.stringify({ ref: "main" })
    });
    if (!res.ok) {
      const t = await res.text();
      console.error("daily dispatch failed " + res.status + " " + t);
    }
  }
};
