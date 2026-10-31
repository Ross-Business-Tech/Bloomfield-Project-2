# Prompts for your team

Copy a prompt, replace the parts in `<angle brackets>`, and paste it into Claude. You don't need to know how to code. Be specific about **who** uses the app and **what they can do**.

## How your team works

1. **One person runs the Kickoff prompt** while the team decides the idea together. Claude builds a first version and opens a *pull request*: a proposed change your teammates can try before it goes live.
2. **Everyone else reviews it.** Open the test link on your phone, then on GitHub click **Files changed > Review changes > Approve**, and **Merge**. Your team's dev site updates a few minutes later. (Your public address stays "Coming soon" until staff reveal all teams at the end.)
3. **Then split up.** Each person picks one feature and starts a **new session** for it. New sessions always start from the team's latest version.
4. **One feature = one session = one pull request.** Small pull requests are easier to review and rarely collide.

## 1. Kickoff (one person, once)

```
We're team <team name> and we're building <app name>.
Who it's for: <the people who will use it>
The problem it solves: <one or two sentences>
In this first version, someone should be able to:
1. <first thing>
2. <second thing>
3. <third thing>
Look and feel: <a few words: colors, mood, an app it should feel like>
Replace the starter page with this first version. Keep it simple enough to finish in an hour.
Use made-up sample data. When it works on a phone, give me a test link, then open a pull
request titled "First version of <app name>".
```

## 2. Start a feature (everyone, new session each time)

```
I'm adding <feature> to our app.
What it should do: <describe it from the user's point of view>
Where it goes: <which page or part of the screen>
It's done when: <how you'll know it works>
Only change what this feature needs, and tell me if you have to touch anything else.
```

## 3. Change something you can see

```
On the <page/screen>, <what's wrong or what you want instead>.
For example: <a concrete example of the result you want>.
```

## 4. Ready to share with the team

```
This is ready. Bring in the team's latest changes, test again, and open a pull request
with a short plain-language summary and my test link.
```

## 5. Review a teammate's pull request

```
Explain pull request #<number> in plain language: what does it change for users,
and is there anything that might break or confuse people? Don't change any code.
```

## 6. Respond to review comments

```
My teammate commented on pull request #<number>: "<paste the comment>".
Please address it and update the pull request.
```

## 7. Something's broken

```
<What you did>, and <what happened instead of what you expected>.
Please find the cause, fix it, and explain what was wrong in one sentence.
```

## 8. Get ready to present

```
We present in <time>. Check the whole app on a phone screen, fix anything that looks broken
or unfinished, and make sure the first screen explains what the app does in one sentence.
```
